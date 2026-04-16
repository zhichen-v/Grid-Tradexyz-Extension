"""

Trade XYZ REST API 模块



基于 Hyperliquid REST API 扩展，支持 XYZ 市场 (美股/商品/外汇):

- 加密货币操作: 复用 ccxt Hyperliquid 实现

- XYZ 市场操作:

  - 行情/持仓查询: 使用直接 HTTP POST 调用 Hyperliquid API (带 dex="xyz" 参数)

  - 下单/取消: 使用 hyperliquid-python-sdk (ccxt 不支援 XYZ 市场符号)



所有 API 端点与 Hyperliquid 完全相同 (https://api.hyperliquid.xyz)

"""



import asyncio

import time

import aiohttp

import eth_account

from datetime import datetime

from typing import Dict, List, Optional, Any

from decimal import Decimal



from hyperliquid.exchange import Exchange as HyperliquidSDKExchange

from hyperliquid.info import Info as HyperliquidSDKInfo



from .hyperliquid_rest import HyperliquidRest

from .tradexyz_base import TradeXYZBase

from ..models import (

    TickerData, OrderBookData, BalanceData, PositionData,

    OrderData, ExchangeInfo, OrderBookLevel,

    OrderSide, OrderType, OrderStatus, PositionSide, MarginMode, ExchangeType

)





class TradeXYZRest(HyperliquidRest, TradeXYZBase):

    """

    Trade XYZ REST API 类



    继承 HyperliquidRest 用于加密货币操作，

    新增直接 HTTP 调用用于 XYZ 市场操作。

    """



    def __init__(self, config=None, logger=None):

        # Use cooperative MRO to avoid double HyperliquidBase.__init__

        super().__init__(config, logger)



        # Load XYZ-specific config (TradeXYZBase may not run _load_xyz_config

        # via MRO if HyperliquidRest.__init__ was called first)

        if not hasattr(self, 'xyz_market_enabled'):

            self.xyz_market_enabled = True

            self.xyz_config = {}

            self._load_xyz_config()



        # HTTP session for direct API calls

        self._http_session: Optional[aiohttp.ClientSession] = None



        # hyperliquid-python-sdk Exchange instance for XYZ order operations

        # (ccxt does not support XYZ market symbols)

        self._xyz_sdk_exchange: Optional[HyperliquidSDKExchange] = None

        self._xyz_sdk_info: Optional[HyperliquidSDKInfo] = None



        # Cache for allMids to avoid redundant calls in rapid succession

        self._xyz_mids_cache: Optional[Dict[str, str]] = None

        self._xyz_mids_cache_ts: float = 0.0

        self._xyz_mids_cache_ttl: float = 2.0  # seconds



    async def connect(self) -> bool:

        """建立连接 - 同时初始化 ccxt、HTTP session 和 XYZ SDK"""

        # 连接 ccxt (用于加密货币)

        result = await super().connect()



        # 创建 HTTP session (用于 XYZ 市场直接 API 调用)

        if not self._http_session or self._http_session.closed:

            self._http_session = aiohttp.ClientSession(

                headers={'Content-Type': 'application/json'}

            )



        # 初始化 hyperliquid-python-sdk (用于 XYZ 市场下单/取消)

        if self.config and self.config.api_key and not self._xyz_sdk_exchange:

            try:

                agent_account = eth_account.Account.from_key(self.config.api_key)

                self._xyz_sdk_exchange = HyperliquidSDKExchange(

                    agent_account,

                    self.base_url,

                    account_address=self.config.wallet_address,

                    perp_dexs=["xyz"],

                )

                self._xyz_sdk_info = HyperliquidSDKInfo(

                    self.base_url, skip_ws=True

                )

                if self.logger:

                    self.logger.info(

                        "XYZ SDK Exchange 初始化成功 "

                        f"(wallet={self.config.wallet_address[:10]}...)"

                    )

            except Exception as exc:

                if self.logger:

                    self.logger.error(f"XYZ SDK Exchange 初始化失败: {exc}")

                self._xyz_sdk_exchange = None

                self._xyz_sdk_info = None



        if self.logger:

            self.logger.info("Trade XYZ REST 连接成功 (ccxt + HTTP + SDK)")



        return result



    async def disconnect(self) -> None:

        """断开连接"""

        # 关闭 HTTP session

        if self._http_session and not self._http_session.closed:

            await self._http_session.close()

            self._http_session = None



        # 清理 XYZ SDK

        self._xyz_sdk_exchange = None

        self._xyz_sdk_info = None



        # 断开 ccxt

        await super().disconnect()



        if self.logger:

            self.logger.info("Trade XYZ REST 连接已断开")



    async def get_balances(self) -> List[BalanceData]:

        """

        获取账户余额（覆写父类方法）



        Hyperliquid 的 USDC 余额在 spotClearinghouseState，

        合约保证金在 clearinghouseState (dex=xyz)。

        两者合并返回完整余额视图。

        """

        try:

            wallet = self.config.wallet_address if self.config else None

            if not wallet:

                if self.logger:

                    self.logger.warning("无法获取余额: 缺少 wallet_address")

                return []



            # 查询现货账户（USDC 实际余额在这里）

            spot_data = await self._xyz_info_request_no_dex(

                "spotClearinghouseState", {"user": wallet}

            )



            # 从 spot balances 中找 USDC

            spot_usdc_total = '0'

            spot_usdc_hold = '0'

            for bal in spot_data.get('balances', []):

                if bal.get('coin') == 'USDC':

                    spot_usdc_total = bal.get('total', '0')

                    spot_usdc_hold = bal.get('hold', '0')

                    break



            # 查询 XYZ 合约账户（获取保证金使用情况）

            xyz_data = await self.get_xyz_clearinghouse_state(wallet)

            xyz_margin = xyz_data.get('crossMarginSummary') or xyz_data.get('marginSummary', {})

            margin_used = xyz_margin.get('totalMarginUsed', '0')

            collateral_total_raw = (

                xyz_margin.get('accountValue')

                or xyz_data.get('accountValue')

                or spot_usdc_total

            )

            collateral_free_raw = (

                xyz_margin.get('withdrawable')

                or xyz_margin.get('availableToWithdraw')

                or xyz_margin.get('availableBalance')

            )



            total = Decimal(str(spot_usdc_total))

            hold = Decimal(str(spot_usdc_hold))

            free = max(total - hold, Decimal('0'))

            used = hold



            collateral_total = Decimal(str(collateral_total_raw))

            collateral_used = Decimal(str(margin_used))

            if collateral_free_raw is not None:

                collateral_free = Decimal(str(collateral_free_raw))

            else:

                collateral_free = max(collateral_total - collateral_used, Decimal('0'))



            balance = BalanceData(

                currency='USDC',

                free=free,

                used=used,

                total=total,

                usd_value=collateral_total,

                timestamp=datetime.now(),

                raw_data={

                    'free': str(free),

                    'used': str(used),

                    'total': str(total),

                    'spot_free': str(free),

                    'spot_total': spot_usdc_total,

                    'spot_hold': spot_usdc_hold,

                    'collateral_free': str(collateral_free),

                    'collateral_total': str(collateral_total),

                    'collateral_used': str(collateral_used),

                    'xyz_margin_used': margin_used,

                }

            )

            return [balance]



        except Exception as e:

            if self.logger:

                self.logger.error(f"获取余额失败: {e}")

            return []



    async def _xyz_info_request_no_dex(self, request_type: str, extra_params: Optional[Dict] = None) -> Any:

        """发送 info 请求（不带 dex=xyz，用于查询主账户如 spotClearinghouseState）"""

        if not self._http_session or self._http_session.closed:

            self._http_session = aiohttp.ClientSession(

                headers={'Content-Type': 'application/json'}

            )

        url = f"{self.base_url}/info"

        payload = {"type": request_type}

        if extra_params:

            payload.update(extra_params)



        async with self._http_session.post(url, json=payload) as response:

            if response.status == 200:

                return await response.json()

            else:

                text = await response.text()

                raise Exception(f"API error {response.status}: {text}")



    # === XYZ 市场直接 API 调用 ===



    async def _xyz_info_request(self, request_type: str, extra_params: Optional[Dict] = None) -> Any:

        """

        发送 XYZ 市场 info 请求



        通过 POST /info 端点，自动附加 dex="xyz" 参数

        """

        if not self._http_session or self._http_session.closed:

            self._http_session = aiohttp.ClientSession(

                headers={'Content-Type': 'application/json'}

            )



        payload = {"type": request_type, "dex": "xyz"}

        if extra_params:

            payload.update(extra_params)



        url = f"{self.base_url}/info"



        try:

            async with self._http_session.post(url, json=payload) as response:

                if response.status == 200:

                    return await response.json()

                else:

                    error_text = await response.text()

                    raise Exception(

                        f"XYZ info 请求失败 ({response.status}): {error_text}")

        except aiohttp.ClientError as e:

            raise Exception(f"XYZ info 请求网络错误: {e}")



    async def get_xyz_meta(self) -> Dict[str, Any]:

        """

        获取 XYZ 市场元数据 (资产配置、杠杆、保证金模式等)



        等同于: POST /info {"type": "metaAndAssetCtxs", "dex": "xyz"}

        """

        return await self._xyz_info_request("metaAndAssetCtxs")



    async def get_xyz_all_mids(self) -> Dict[str, str]:

        """

        获取 XYZ 市场所有资产的中间价



        等同于: POST /info {"type": "allMids", "dex": "xyz"}

        返回: {"xyz:TSLA": "250.5", "xyz:AAPL": "180.3", ...}

        """

        return await self._xyz_info_request("allMids")



    async def get_xyz_l2_book(self, coin: str, n_sig_figs: int = 5) -> Dict[str, Any]:

        """

        获取 XYZ 市场订单簿



        Args:

            coin: 资产名称 (如 "TSLA"，会自动加 xyz: 前缀)

            n_sig_figs: 有效数字位数 (最多5)

        """

        xyz_coin = self.to_xyz_coin(coin)

        return await self._xyz_info_request("l2Book", {

            "coin": xyz_coin,

            "nSigFigs": n_sig_figs

        })



    async def get_xyz_candles(

        self, coin: str, interval: str, start_time: int, end_time: int

    ) -> List[Dict]:

        """

        获取 XYZ 市场 K 线数据



        candleSnapshot 使用嵌套 req 格式（与其他端点不同）:

        {"type": "candleSnapshot", "req": {"coin": ..., "interval": ..., ...}}



        Args:

            coin: 资产名称

            interval: K线周期 (1m, 5m, 15m, 1h, 4h, 1d, 等)

            start_time: 开始时间戳 (毫秒)

            end_time: 结束时间戳 (毫秒)

        """

        xyz_coin = self.to_xyz_coin(coin)



        if not self._http_session or self._http_session.closed:

            self._http_session = aiohttp.ClientSession(

                headers={'Content-Type': 'application/json'}

            )



        payload = {

            "type": "candleSnapshot",

            "req": {

                "coin": xyz_coin,

                "interval": interval,

                "startTime": start_time,

                "endTime": end_time,

            },

        }



        url = f"{self.base_url}/info"

        try:

            async with self._http_session.post(url, json=payload) as response:

                if response.status == 200:

                    return await response.json()

                else:

                    error_text = await response.text()

                    raise Exception(

                        f"XYZ candleSnapshot 请求失败 ({response.status}): {error_text}")

        except aiohttp.ClientError as e:

            raise Exception(f"XYZ candleSnapshot 网络错误: {e}")



    async def get_xyz_clearinghouse_state(self, user_address: str) -> Dict[str, Any]:

        """

        获取 XYZ 市场用户持仓/账户状态



        Args:

            user_address: 用户钱包地址 (非 agent wallet)

        """

        return await self._xyz_info_request("clearinghouseState", {

            "user": user_address

        })



    async def get_xyz_open_orders(self, user_address: str) -> List[Dict]:

        """

        获取 XYZ 市场用户挂单



        Args:

            user_address: 用户钱包地址

        """

        return await self._xyz_info_request("frontendOpenOrders", {

            "user": user_address

        })



    async def get_xyz_user_fills(self, user_address: str) -> List[Dict]:

        """

        获取 XYZ 市场用户成交记录



        Args:

            user_address: 用户钱包地址

        """

        return await self._xyz_info_request("userFills", {

            "user": user_address

        })



    async def get_xyz_funding_history(self, coin: str, start_time: int, end_time: Optional[int] = None) -> List[Dict]:

        """

        获取 XYZ 市场资金费率历史



        Args:

            coin: 资产名称

            start_time: 开始时间戳 (毫秒)

            end_time: 结束时间戳 (毫秒，可选)

        """

        xyz_coin = self.to_xyz_coin(coin)

        params = {

            "coin": xyz_coin,

            "startTime": start_time

        }

        if end_time:

            params["endTime"] = end_time

        return await self._xyz_info_request("fundingHistory", params)



    async def get_xyz_predicted_fundings(self) -> List[Dict]:

        """获取 XYZ 市场预测资金费率"""

        return await self._xyz_info_request("predictedFundings")



    async def get_xyz_perp_dex_limits(self) -> Dict[str, Any]:

        """获取 XYZ 市场 OI 上限"""

        return await self._xyz_info_request("perpDexLimits")



    # === XYZ 市场下单/取消 (通过 hyperliquid-python-sdk) ===



    @staticmethod

    def _round_to_sig_figs(value: float, sig_figs: int = 5) -> float:

        """将浮点数取整到指定有效数字位数（Hyperliquid 要求最多 5 位有效数字）"""

        if value == 0:

            return 0.0

        import math

        magnitude = math.floor(math.log10(abs(value)))

        factor = 10 ** (sig_figs - 1 - magnitude)

        return round(value * factor) / factor



    def _ensure_xyz_sdk(self) -> None:

        """确保 XYZ SDK 已初始化"""

        if not self._xyz_sdk_exchange:

            raise Exception(

                "XYZ SDK Exchange 未初始化，无法对 XYZ 市场下单。"

                "请检查 api_key (private_key) 和 wallet_address 配置。"

            )



    async def create_order(

        self,

        symbol: str,

        side: OrderSide,

        order_type: OrderType,

        amount: Decimal,

        price: Optional[Decimal] = None,

        params: Optional[Dict[str, Any]] = None

    ) -> OrderData:

        """创建订单 - XYZ 符号走 SDK，加密货币走 ccxt"""

        if not self.is_xyz_symbol(symbol):

            return await super().create_order(symbol, side, order_type, amount, price, params)



        self._ensure_xyz_sdk()

        xyz_coin = self.to_xyz_coin(symbol)

        is_buy = side == OrderSide.BUY



        # SDK order_type 格式

        if order_type == OrderType.MARKET:

            sdk_order_type = {"limit": {"tif": "Ioc"}}

        else:

            sdk_order_type = {"limit": {"tif": "Gtc"}}



        # Hyperliquid 要求价格最多 5 位有效数字，否则报 tick size 错误

        limit_px = self._round_to_sig_figs(float(price)) if price else 0.0



        # 通过 SDK 下单 (同步调用，放入线程池)

        order_result = await asyncio.get_event_loop().run_in_executor(

            None,

            lambda: self._xyz_sdk_exchange.order(

                name=xyz_coin,

                is_buy=is_buy,

                sz=float(amount),

                limit_px=limit_px,

                order_type=sdk_order_type,

            )

        )



        return self._parse_xyz_sdk_order_result(order_result, symbol, side, amount, price)



    async def cancel_order(self, order_id: str, symbol: str) -> OrderData:

        """取消订单 - XYZ 符号走 SDK，加密货币走 ccxt"""

        if not self.is_xyz_symbol(symbol):

            return await super().cancel_order(order_id, symbol)



        self._ensure_xyz_sdk()

        xyz_coin = self.to_xyz_coin(symbol)



        # oid 需要是 int

        oid = int(order_id)



        cancel_result = await asyncio.get_event_loop().run_in_executor(

            None,

            lambda: self._xyz_sdk_exchange.cancel(name=xyz_coin, oid=oid)

        )



        return self._parse_xyz_sdk_cancel_result(cancel_result, order_id, symbol)



    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[OrderData]:

        """取消所有订单 - XYZ 符号走 SDK，加密货币走 ccxt"""

        if symbol and not self.is_xyz_symbol(symbol):

            return await super().cancel_all_orders(symbol)



        results: List[OrderData] = []



        # 如果未指定 symbol 或是加密货币，也取消 ccxt 端的订单

        if symbol is None:

            try:

                crypto_results = await super().cancel_all_orders(None)

                results.extend(crypto_results)

            except Exception as e:

                if self.logger:

                    self.logger.warning(f"取消加密货币订单时出错: {e}")



        # 取消 XYZ 市场订单

        if self.xyz_market_enabled and self._xyz_sdk_exchange:

            try:

                xyz_orders = await self._get_xyz_open_orders_list(symbol)

                for order in xyz_orders:

                    try:

                        cancel_result = await self.cancel_order(

                            str(order.get('oid', '')),

                            self.to_xyz_coin(order.get('coin', ''))

                        )

                        results.append(cancel_result)

                    except Exception as e:

                        if self.logger:

                            self.logger.error(f"取消 XYZ 订单失败 {order.get('oid')}: {e}")

            except Exception as e:

                if self.logger:

                    self.logger.error(f"获取 XYZ 挂单列表失败: {e}")



        return results



    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderData]:

        """获取开放订单 - 自动判断市场"""

        if symbol and not self.is_xyz_symbol(symbol):

            return await super().get_open_orders(symbol)



        results: List[OrderData] = []



        # 如果未指定 symbol，也查询加密货币端

        if symbol is None:

            try:

                crypto_orders = await super().get_open_orders(None)

                results.extend(crypto_orders)

            except Exception as e:

                if self.logger:

                    self.logger.warning(f"查询加密货币挂单时出错: {e}")



        # 查询 XYZ 市场挂单

        if self.xyz_market_enabled:

            try:

                xyz_orders = await self._get_xyz_open_orders_list(symbol)

                for o in xyz_orders:

                    results.append(self._parse_xyz_frontend_order(o))

            except Exception as e:

                if self.logger:

                    self.logger.error(f"查询 XYZ 挂单失败: {e}")



        return results



    async def get_order(self, order_id: str, symbol: str) -> OrderData:

        """获取订单信息 - XYZ 符号优先走 open orders / user fills 查询。"""

        if not self.is_xyz_symbol(symbol):

            return await super().get_order(order_id, symbol)



        order_id = str(order_id)



        # 1. 订单仍在挂单列表中

        open_orders = await self._get_xyz_open_orders_list(symbol)

        for order_raw in open_orders:

            if str(order_raw.get("oid", "")) == order_id:

                return self._parse_xyz_frontend_order(order_raw)



        fills: List[Dict[str, Any]] = []

        if self.config and self.config.wallet_address:

            try:

                fills = await self.get_xyz_user_fills(self.config.wallet_address)

            except Exception as e:

                if self.logger:

                    self.logger.warning(f"?? XYZ userFills ?? {order_id}: {e}")



        # 2. ??????????????? userFills ??????????????

        matched_fills = [

            fill_raw for fill_raw in fills

            if str(fill_raw.get("oid", "")) == order_id

        ]

        if matched_fills:

            first_fill = matched_fills[0]

            side_str = str(first_fill.get("side", "")).upper()

            side = OrderSide.BUY if side_str == "B" else OrderSide.SELL



            total_amount = Decimal("0")

            total_cost = Decimal("0")

            latest_timestamp = None

            fallback_price = Decimal("0")



            for fill_raw in matched_fills:

                fill_amount = self._safe_decimal(

                    fill_raw.get("sz", fill_raw.get("filledSz", "0"))

                )

                fill_price = self._safe_decimal(

                    fill_raw.get("px", fill_raw.get("avgPx", fill_raw.get("price", "0")))

                )

                fill_timestamp = self._safe_parse_timestamp(

                    fill_raw.get("time", fill_raw.get("timestamp"))

                )



                total_amount += fill_amount

                if fill_amount > 0 and fill_price > 0:

                    total_cost += fill_amount * fill_price

                    fallback_price = fill_price

                elif fill_price > 0:

                    fallback_price = fill_price



                if fill_timestamp and (latest_timestamp is None or fill_timestamp > latest_timestamp):

                    latest_timestamp = fill_timestamp



            average_price = (

                (total_cost / total_amount)

                if total_amount > 0 and total_cost > 0

                else fallback_price

            )

            timestamp = latest_timestamp or datetime.now()



            return OrderData(

                id=order_id,

                client_id=None,

                symbol=symbol,

                side=side,

                type=OrderType.LIMIT,

                amount=total_amount,

                price=average_price if average_price > 0 else None,

                filled=total_amount,

                remaining=Decimal("0"),

                cost=total_cost if total_cost > 0 else Decimal("0"),

                average=average_price if average_price > 0 else None,

                status=OrderStatus.UNKNOWN,

                timestamp=timestamp,

                updated=timestamp,

                fee=None,

                trades=[],

                params={"user_fill_snapshot_only": True},

                raw_data={"fills": matched_fills, "order_id": order_id},

            )



        # 3. TradeXYZ 没有稳定的单笔查单接口；订单既不在挂单中、也未命中 fills，

        # 对网格系统按“已离开挂单簿”处理，交由上层补充反手单。

        if self.logger:

            self.logger.warning(

                f"XYZ 订单 {order_id} 未命中 open orders / userFills，按已成交处理"

            )



        return OrderData(

            id=order_id,

            client_id=None,

            symbol=symbol,

            side=OrderSide.BUY,

            type=OrderType.LIMIT,

            amount=Decimal("0"),

            price=None,

            filled=Decimal("0"),

            remaining=Decimal("0"),

            cost=Decimal("0"),

            average=None,

            status=OrderStatus.UNKNOWN,

            timestamp=datetime.now(),

            updated=datetime.now(),

            fee=None,

            trades=[],

            params={"inferred": True},

            raw_data={"inferred": True, "order_id": order_id},

        )



    async def _get_xyz_open_orders_list(self, symbol: Optional[str] = None) -> List[Dict]:

        """获取 XYZ 市场原始挂单列表，可按 symbol 过滤"""

        if not self.config or not self.config.wallet_address:

            return []



        all_orders = await self.get_xyz_open_orders(self.config.wallet_address)



        if symbol:

            base = self._extract_base_symbol(symbol)

            all_orders = [

                o for o in all_orders

                if self._extract_base_symbol(o.get('coin', '')) == base

            ]



        return all_orders



    def _parse_xyz_sdk_order_result(

        self,

        result: Dict[str, Any],

        symbol: str,

        side: OrderSide,

        amount: Decimal,

        price: Optional[Decimal],

    ) -> OrderData:

        """解析 SDK order() 返回结果为 OrderData"""

        # 检查错误

        if isinstance(result, dict) and result.get("status") == "err":

            raise Exception(f"XYZ 下单失败: {result.get('response', result)}")



        statuses = (

            result.get("response", {}).get("data", {}).get("statuses", [])

            if isinstance(result, dict) else []

        )



        if not statuses:

            raise Exception(f"XYZ 下单无状态返回: {result}")



        status_item = statuses[0]

        if "error" in status_item:

            raise Exception(f"XYZ 下单错误: {status_item['error']}")



        # 提取 resting (挂单) 或 filled (立即成交) 信息

        resting = status_item.get("resting", {})

        filled = status_item.get("filled", {})



        order_id = str(resting.get("oid", "") or filled.get("oid", ""))

        order_status = OrderStatus.OPEN if resting else OrderStatus.FILLED



        return OrderData(

            id=order_id,

            client_id=None,

            symbol=symbol,

            side=side,

            type=OrderType.LIMIT,

            amount=amount,

            price=price,

            filled=None if resting else amount,

            remaining=amount if resting else Decimal("0"),

            cost=None,

            average=self._safe_decimal(filled.get("avgPx")) if filled else None,

            status=order_status,

            timestamp=datetime.now(),

            updated=None,

            fee=None,

            trades=[],

            params={},

            raw_data=result,

        )



    def _parse_xyz_sdk_cancel_result(

        self, result: Dict[str, Any], order_id: str, symbol: str

    ) -> OrderData:

        """解析 SDK cancel() 返回结果为 OrderData"""

        statuses = (

            result.get("response", {}).get("data", {}).get("statuses", [])

            if isinstance(result, dict) else []

        )



        if statuses and statuses[0] == "success":

            status = OrderStatus.CANCELED

        else:

            raise Exception(f"XYZ 取消订单失败: {statuses or result}")



        return OrderData(

            id=order_id,

            client_id=None,

            symbol=symbol,

            side=OrderSide.BUY,

            type=OrderType.LIMIT,

            amount=None,

            price=None,

            filled=None,

            remaining=None,

            cost=None,

            average=None,

            status=status,

            timestamp=datetime.now(),

            updated=None,

            fee=None,

            trades=[],

            params={},

            raw_data=result,

        )



    def _parse_xyz_frontend_order(self, order_raw: Dict[str, Any]) -> OrderData:

        """解析 frontendOpenOrders 返回的单个挂单为 OrderData"""

        coin = order_raw.get("coin", "")

        base = self.from_xyz_coin(coin)

        symbol = f"{base}/USD:PERP"



        side_str = order_raw.get("side", "")

        side = OrderSide.BUY if side_str == "B" else OrderSide.SELL



        return OrderData(

            id=str(order_raw.get("oid", "")),

            client_id=None,

            symbol=symbol,

            side=side,

            type=OrderType.LIMIT,

            amount=self._safe_decimal(order_raw.get("sz")),

            price=self._safe_decimal(order_raw.get("limitPx")),

            filled=Decimal("0"),

            remaining=self._safe_decimal(order_raw.get("sz")),

            cost=None,

            average=None,

            status=OrderStatus.OPEN,

            timestamp=self._safe_parse_timestamp(order_raw.get("timestamp")),

            updated=None,

            fee=None,

            trades=[],

            params={},

            raw_data=order_raw,

        )



    # === 覆写的统一接口方法 (自动判断市场) ===



    async def get_ticker(self, symbol: str) -> TickerData:

        """

        获取行情 - 自动判断是加密货币还是 XYZ 市场



        对于 XYZ 市场资产，使用 allMids API 获取中间价

        对于加密货币，使用 ccxt fetch_ticker

        """

        if self.is_xyz_symbol(symbol):

            return await self._get_xyz_ticker(symbol)



        # 加密货币: 使用 ccxt

        return await super().get_ticker(symbol)



    async def _get_cached_xyz_all_mids(self) -> Dict[str, str]:

        """获取 XYZ allMids（带短期缓存避免重复请求）"""

        now = time.monotonic()

        if self._xyz_mids_cache is not None and (now - self._xyz_mids_cache_ts) < self._xyz_mids_cache_ttl:

            return self._xyz_mids_cache

        self._xyz_mids_cache = await self.get_xyz_all_mids()

        self._xyz_mids_cache_ts = now

        return self._xyz_mids_cache



    async def _get_xyz_ticker(self, symbol: str) -> TickerData:

        """获取 XYZ 市场行情"""

        xyz_coin = self.to_xyz_coin(symbol)

        base = self.from_xyz_coin(xyz_coin)



        # 获取所有中间价（带缓存）

        all_mids = await self._get_cached_xyz_all_mids()

        mid_price = all_mids.get(xyz_coin)



        if mid_price is None:

            raise Exception(f"未找到 {xyz_coin} 的价格数据")



        price = self._safe_decimal(mid_price)



        return TickerData(

            symbol=symbol,

            bid=price,

            ask=price,

            bid_size=None,

            ask_size=None,

            last=price,

            open=None,

            high=None,

            low=None,

            close=price,

            volume=None,

            quote_volume=None,

            trades_count=None,

            change=None,

            percentage=None,

            funding_rate=None,

            predicted_funding_rate=None,

            funding_time=None,

            next_funding_time=None,

            funding_interval=None,

            index_price=None,

            mark_price=None,

            oracle_price=None,

            open_interest=None,

            open_interest_value=None,

            delivery_date=None,

            high_time=None,

            low_time=None,

            start_time=None,

            end_time=None,

            contract_id=None,

            contract_name=symbol,

            base_currency=base,

            quote_currency="USD",

            contract_size=None,

            tick_size=None,

            lot_size=None,

            timestamp=datetime.now(),

            exchange_timestamp=None,

            received_timestamp=datetime.now(),

            processed_timestamp=None,

            sent_timestamp=None,

            raw_data={"xyz_coin": xyz_coin, "mid": mid_price}

        )



    async def get_orderbook(self, symbol: str, limit: Optional[int] = None) -> OrderBookData:

        """获取订单簿 - 自动判断市场"""

        if self.is_xyz_symbol(symbol):

            return await self._get_xyz_orderbook(symbol)



        return await super().get_orderbook(symbol, limit)



    async def _get_xyz_orderbook(self, symbol: str) -> OrderBookData:

        """获取 XYZ 市场订单簿"""

        l2_data = await self.get_xyz_l2_book(symbol)



        levels = l2_data.get('levels', [[], []])

        bids_raw = levels[0] if len(levels) > 0 else []

        asks_raw = levels[1] if len(levels) > 1 else []



        bids = [

            OrderBookLevel(

                price=self._safe_decimal(b.get('px')),

                size=self._safe_decimal(b.get('sz'))

            )

            for b in bids_raw

        ]



        asks = [

            OrderBookLevel(

                price=self._safe_decimal(a.get('px')),

                size=self._safe_decimal(a.get('sz'))

            )

            for a in asks_raw

        ]



        return OrderBookData(

            symbol=symbol,

            bids=bids,

            asks=asks,

            timestamp=datetime.now(),

            nonce=None,

            raw_data=l2_data

        )



    async def get_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:

        """

        获取持仓 - 自动判断市场



        如果请求的符号包含 XYZ 资产，会同时查询 XYZ 市场持仓

        """

        positions = []



        # 分离加密货币和 XYZ 符号

        crypto_symbols = []

        xyz_symbols = []



        if symbols:

            for s in symbols:

                if self.is_xyz_symbol(s):

                    xyz_symbols.append(s)

                else:

                    crypto_symbols.append(s)

        else:

            # 无指定时查询两个市场

            crypto_symbols = None

            xyz_symbols = None



        # 获取加密货币持仓

        if crypto_symbols is not None:

            if crypto_symbols:  # 有指定的加密货币符号

                crypto_positions = await super().get_positions(crypto_symbols)

                positions.extend(crypto_positions)

        else:

            crypto_positions = await super().get_positions()

            positions.extend(crypto_positions)



        # 获取 XYZ 市场持仓

        if self.xyz_market_enabled and (xyz_symbols is not None or symbols is None):

            xyz_positions = await self._get_xyz_positions(xyz_symbols)

            positions.extend(xyz_positions)



        return positions



    async def _get_xyz_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:

        """获取 XYZ 市场持仓"""

        if not self.config or not self.config.wallet_address:

            if self.logger:

                self.logger.warning("无法获取 XYZ 持仓: 未配置钱包地址")

            return []



        try:

            state = await self.get_xyz_clearinghouse_state(self.config.wallet_address)

            asset_positions = state.get('assetPositions', [])



            positions = []

            for pos_info in asset_positions:

                position_data = pos_info.get('position', {})

                coin = position_data.get('coin', '')



                # 解析符号

                base = self.from_xyz_coin(coin)

                pos_symbol = f"{base}/USD:PERP"



                # 过滤: 支持 "TSLA" 和 "TSLA/USD:PERP" 两种格式

                if symbols:

                    normalized = {self._extract_base_symbol(s) for s in symbols}

                    if base not in normalized and pos_symbol not in symbols:

                        continue



                size = self._safe_decimal(position_data.get('szi', '0'))

                if size is None or size == 0:

                    continue



                side = PositionSide.LONG if size > 0 else PositionSide.SHORT



                positions.append(PositionData(

                    symbol=pos_symbol,

                    side=side,

                    size=abs(size),

                    entry_price=self._safe_decimal(position_data.get('entryPx')),

                    mark_price=self._safe_decimal(position_data.get('markPx')),

                    current_price=self._safe_decimal(position_data.get('markPx')),

                    unrealized_pnl=self._safe_decimal(position_data.get('unrealizedPnl')),

                    realized_pnl=self._safe_decimal(position_data.get('cumFunding', {}).get('allTime')),

                    percentage=None,

                    leverage=self._safe_int(position_data.get('leverage', {}).get('value', 1)),

                    margin_mode=MarginMode.ISOLATED if position_data.get('leverage', {}).get('type') == 'isolated' else MarginMode.CROSS,

                    margin=self._safe_decimal(position_data.get('marginUsed')),

                    liquidation_price=self._safe_decimal(position_data.get('liquidationPx')),

                    timestamp=datetime.now(),

                    raw_data=pos_info

                ))



            return positions



        except Exception as e:

            if self.logger:

                self.logger.error(f"获取 XYZ 持仓失败: {e}")

            return []



    async def get_exchange_info(self) -> ExchangeInfo:

        """获取交易所信息"""

        return ExchangeInfo(

            name="Trade XYZ",

            id="tradexyz",

            type=ExchangeType.PERPETUAL,

            supported_features=[

                "perpetual_trading", "websocket", "orderbook",

                "ticker", "ohlcv", "user_stream",

                "xyz_stocks", "xyz_commodities", "xyz_fx"

            ],

            rate_limits=self.config.rate_limits if self.config else {},

            precision=self.config.precision if self.config else {},

            fees={},

            markets=self.exchange.markets if self.exchange else {},

            status="operational",

            timestamp=datetime.now()

        )

