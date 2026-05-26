# -*- coding: utf-8 -*-
"""
===================================
ETF 配置文件 - 18只精选ETF池
===================================

配置说明：
- A股宽基：沪深300、中证500、中证1000
- 成长弹性：创业板50、科创50
- 红利价值：红利ETF、深证红利
- 海外方向：纳指ETF
- 港股方向：恒生ETF、港股科技、港股红利
- 商品资源：黄金、豆粕、有色、能源
- 真防守：国债ETF、十年国债ETF、货币ETF

目标：进攻资产负责弹性，防守资产负责活下来
"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Dict


class ETFCategory(Enum):
    """ETF类别枚举"""
    A_BROAD = "A股宽基"
    GROWTH = "成长弹性"
    DIVIDEND = "红利价值"
    OVERSEAS = "海外方向"
    HK = "港股方向"
    COMMODITY = "商品资源"
    DEFENSE = "真防守"


@dataclass
class ETFInfo:
    """ETF信息"""
    code: str              # 代码
    name: str              # 名称
    category: ETFCategory  # 类别
    is_etf: bool = True    # 是否为ETF（有些可能是LOF）
    description: str = ""  # 描述
    
    def to_dict(self) -> Dict:
        return {
            'code': self.code,
            'name': self.name,
            'category': self.category.value,
            'description': self.description
        }


# ETF池配置（18只精选）
ETF_POOL: List[ETFInfo] = [
    # A股宽基（3只）
    ETFInfo(
        code="510300",
        name="沪深300ETF",
        category=ETFCategory.A_BROAD,
        description="沪深300指数，大盘蓝筹代表"
    ),
    ETFInfo(
        code="510500",
        name="中证500ETF",
        category=ETFCategory.A_BROAD,
        description="中证500指数，中盘成长代表"
    ),
    ETFInfo(
        code="159629",
        name="中证1000ETF",
        category=ETFCategory.A_BROAD,
        description="中证1000指数，小盘成长代表"
    ),
    
    # 成长弹性（2只）
    ETFInfo(
        code="159915",
        name="创业板50ETF",
        category=ETFCategory.GROWTH,
        description="创业板50指数，科技成长龙头"
    ),
    ETFInfo(
        code="588000",
        name="科创50ETF",
        category=ETFCategory.GROWTH,
        description="科创50指数，硬科技核心"
    ),
    
    # 红利价值（2只）
    ETFInfo(
        code="510880",
        name="红利ETF",
        category=ETFCategory.DIVIDEND,
        description="红利指数，高股息策略"
    ),
    ETFInfo(
        code="159905",
        name="深证红利ETF",
        category=ETFCategory.DIVIDEND,
        description="深证红利指数，深市高股息"
    ),
    
    # 海外方向（1只）
    ETFInfo(
        code="513100",
        name="纳指ETF",
        category=ETFCategory.OVERSEAS,
        description="纳斯达克100指数，美股科技"
    ),
    
    # 港股方向（3只）
    ETFInfo(
        code="159920",
        name="恒生ETF",
        category=ETFCategory.HK,
        description="恒生指数，港股核心"
    ),
    ETFInfo(
        code="513180",
        name="港股科技ETF",
        category=ETFCategory.HK,
        description="恒生科技指数，港股科技"
    ),
    ETFInfo(
        code="513580",
        name="港股红利ETF",
        category=ETFCategory.HK,
        description="港股高股息策略"
    ),
    
    # 商品资源（4只）
    ETFInfo(
        code="518880",
        name="黄金ETF",
        category=ETFCategory.COMMODITY,
        description="黄金ETF，避险资产"
    ),
    ETFInfo(
        code="159985",
        name="豆粕ETF",
        category=ETFCategory.COMMODITY,
        description="豆粕期货ETF，农产品"
    ),
    ETFInfo(
        code="512400",
        name="有色ETF",
        category=ETFCategory.COMMODITY,
        description="有色金属ETF，周期品种"
    ),
    ETFInfo(
        code="159981",
        name="能源ETF",
        category=ETFCategory.COMMODITY,
        description="能源ETF，油气煤炭"
    ),
    
    # 真防守（3只）
    ETFInfo(
        code="511010",
        name="国债ETF",
        category=ETFCategory.DEFENSE,
        description="5年期国债ETF，稳健防守"
    ),
    ETFInfo(
        code="511260",
        name="十年国债ETF",
        category=ETFCategory.DEFENSE,
        description="10年期国债ETF，长期防守"
    ),
    ETFInfo(
        code="511880",
        name="货币ETF",
        category=ETFCategory.DEFENSE,
        description="货币ETF，现金管理"
    ),
]


def get_etf_pool() -> List[ETFInfo]:
    """获取ETF池"""
    return ETF_POOL


def get_etf_by_category(category: ETFCategory) -> List[ETFInfo]:
    """按类别获取ETF"""
    return [etf for etf in ETF_POOL if etf.category == category]


def get_etf_by_code(code: str) -> ETFInfo:
    """按代码获取ETF"""
    for etf in ETF_POOL:
        if etf.code == code:
            return etf
    return None


def get_etf_codes() -> List[str]:
    """获取所有ETF代码"""
    return [etf.code for etf in ETF_POOL]


def get_category_etf_map() -> Dict[ETFCategory, List[ETFInfo]]:
    """获取按类别分组的ETF映射"""
    result = {}
    for category in ETFCategory:
        result[category] = get_etf_by_category(category)
    return result


if __name__ == "__main__":
    print("ETF池配置：")
    print(f"总计: {len(ETF_POOL)} 只ETF\n")
    
    for category in ETFCategory:
        etfs = get_etf_by_category(category)
        print(f"{category.value} ({len(etfs)}只):")
        for etf in etfs:
            print(f"  - {etf.code} {etf.name}: {etf.description}")
        print()
