# -*- coding: utf-8 -*-
"""
===================================
AmazingDataFetcher - 星耀数智数据源
===================================

数据来源：中国银河证券 星耀数智 AmazingData 行情平台（基于 tgw 行情网关）
特点：
- 交易所直连数据，稳定性高，无爬虫封禁风险
- 支持 A股/ETF/可转债/期货/期权/港股通
- 依赖 TGW 账号（.env 配置 TGW_USERNAME/TGW_PASSWORD/TGW_HOST/TGW_PORT）

设计：
- 懒登录：首次使用时登录，登录失败抛出异常让 DataFetcherManager 切换
- 未配置 TGW 凭证时不注册（_init_default_fetchers 检查）
- 指标计算走 AmazingData numba 算子（见 numba_indicators.py）
"""

import contextlib
import io
import logging
import os
import threading
from typing import Optional, Dict, Any, List

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code

logger = logging.getLogger(__name__)

# 幂等加载 .env（项目入口已调用 setup_env 时无副作用；独立使用本模块也能读到凭证）
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# === 配置 ===
_TGW_HOST = os.getenv("TGW_HOST", "").strip()
_TGW_PORT = os.getenv("TGW_PORT", "").strip()
_TGW_USERNAME = os.getenv("TGW_USERNAME", "").strip()
_TGW_PASSWORD = os.getenv("TGW_PASSWORD", "").strip()


def tgw_configured() -> bool:
    """是否已配置 TGW 登录凭证。"""
    return bool(_TGW_HOST and _TGW_PORT and _TGW_USERNAME and _TGW_PASSWORD)


def _code_to_tgw_format(code: str) -> str:
    """
    将标准 6 位代码转换为 tgw 格式（600519 -> 600519.SH）。

    Returns:
        tgw 格式代码；不支持的代码返回 None
    """
    code = normalize_stock_code(code)
    if not code.isdigit() or len(code) != 6:
        return None
    if code.startswith(("6", "51", "52", "56", "58")):
        return f"{code}.SH"
    if code.startswith(("0", "3", "15", "16", "18")):
        return f"{code}.SZ"
    return None  # 北交所等其他市场暂不支持


class AmazingDataFetcher(BaseFetcher):
    """
    AmazingData 数据源实现

    优先级：0（最高，需要 TGW 凭证）
    数据来源：星耀数智行情平台（query_kline）
    """

    name = "AmazingDataFetcher"
    # 默认最高优先级（-1，高于 Akshare/Efinance 的 0/1）；
    # 未配置凭证时不注册。失败时由 DataFetcherManager 自动切换到下一数据源
    priority = int(os.getenv("AMAZINGDATA_PRIORITY", "-1"))

    # 登录单例
    _login_lock = threading.Lock()
    _login_attempted = False
    _login_success = False
    _market_data = None
    _calendar = None

    def __init__(self):
        super().__init__()
        if not tgw_configured():
            raise DataFetchError(
                "AmazingDataFetcher 未配置 TGW 凭证（TGW_HOST/TGW_PORT/TGW_USERNAME/TGW_PASSWORD）"
            )

    # ---------- 登录管理 ----------

    @classmethod
    def _ensure_login(cls) -> None:
        """确保已登录 tgw 并拿到 MarketData 实例。"""
        with cls._login_lock:
            if cls._login_success and cls._market_data is not None:
                return
            if cls._login_attempted and not cls._login_success:
                raise DataFetchError("AmazingData 登录已尝试失败，跳过该数据源")

            cls._login_attempted = True
            try:
                # 登录会打印大量 logon json，临时静默
                with contextlib.redirect_stdout(io.StringIO()):
                    import AmazingData as ad

                    ad.login(
                        username=_TGW_USERNAME,
                        password=_TGW_PASSWORD,
                        host=_TGW_HOST,
                        port=int(_TGW_PORT),
                    )
                    base = ad.BaseData()
                    cal = base.get_calendar()
                    market = ad.MarketData(cal)
                cls._calendar = cal
                cls._market_data = market
                cls._login_success = True
                logger.info(f"AmazingData 登录成功，交易日历 {len(cal)} 天")
            except SystemExit:
                # ad.login 失败时会 sys.exit(0)，转为异常交给上层切换数据源
                cls._login_success = False
                raise DataFetchError("AmazingData 登录失败（账号/密码/网络），切换其他数据源")
            except Exception as e:
                cls._login_success = False
                raise DataFetchError(f"AmazingData 登录异常: {e}") from e

    @classmethod
    def ensure_login(cls) -> None:
        """公共登录入口（供其他模块复用登录态）。未配置凭证时不报错。"""
        if not tgw_configured():
            raise DataFetchError("AmazingData 未配置 TGW 凭证")
        cls._ensure_login()

    @classmethod
    def get_info_data(cls):
        """获取 InfoData 实例（复用已建立的登录态）。"""
        cls._ensure_login()
        with contextlib.redirect_stdout(io.StringIO()):
            import AmazingData as ad
            return ad.InfoData()

    # ---------- BaseFetcher 接口 ----------

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 AmazingData 获取日 K 数据。

        数据来源：MarketData.query_kline()
        返回原始列：code, kline_time, open, high, low, close, volume, amount
        """
        self._ensure_login()

        tgw_code = _code_to_tgw_format(stock_code)
        if tgw_code is None:
            raise DataFetchError(
                f"AmazingDataFetcher 不支持的代码 {stock_code}（仅支持沪深 A 股与 ETF）"
            )

        begin = int(start_date.replace("-", ""))
        end = int(end_date.replace("-", ""))

        logger.info(f"[API调用] query_kline({tgw_code}, {begin}~{end}, period=day)")
        try:
            from AmazingData.utils.constant import Period

            kline_dict = self._market_data.query_kline(
                [tgw_code],
                begin_date=begin,
                end_date=end,
                period=Period.day.value,
            )
        except SystemExit:
            raise DataFetchError("AmazingData 查询被中断")
        except Exception as e:
            raise DataFetchError(f"AmazingData query_kline 失败: {e}") from e

        df = kline_dict.get(tgw_code)
        if df is None or df.empty:
            raise DataFetchError(f"AmazingData 未返回 {tgw_code} 的数据")

        logger.info(f"[API返回] query_kline({tgw_code}) 成功: rows={len(df)}")
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 AmazingData 数据。

        AmazingData 列：code, kline_time, open, high, low, close, volume, amount
        映射到：date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()
        df = df.rename(columns={"kline_time": "date"})

        if "date" not in df.columns:
            raise DataFetchError("AmazingData 数据缺少日期列")

        df["date"] = pd.to_datetime(df["date"])

        # 计算涨跌幅（数据源不直接提供）
        df["pct_chg"] = df["close"].pct_change() * 100

        # 只保留需要的列
        keep_cols = ["date"] + [c for c in STANDARD_COLUMNS if c != "date"]
        existing_cols = [c for c in keep_cols if c in df.columns]
        return df[existing_cols]

    def get_realtime_quote(self, stock_code: str):
        """
        AmazingData 快照为分时 5 档数据（单日单股 5000+ 行），
        不适合实时行情单点查询，返回 None 交由其他数据源处理。
        """
        return None

    def get_main_indices(self, region: str = "cn") -> Optional[List[Dict[str, Any]]]:
        """AmazingData 指数快照接口未封装，返回 None。"""
        return None

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """AmazingData 未提供市场统计接口，返回 None。"""
        return None

    def get_sector_rankings(self, n: int = 5):
        """AmazingData 未提供板块排行接口，返回 None。"""
        return None


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    f = AmazingDataFetcher()
    df = f.get_daily_data("600519", start_date="2026-07-01", end_date="2026-08-07")
    print(df.tail(5))
    print("\n列:", list(df.columns))
