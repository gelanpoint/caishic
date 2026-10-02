"""市场结构：摊位 / 品类 / 商品 / 基准价（`docs/sim-design.md` §2.2 的 `env/market.py`）。

**为什么仿真要自带一份市场结构，而不是读主系统的种子数据**：
读主系统就得 `import app`（或在仿真启动前先起服务），前者破坏唯一耦合面的边界，
后者让"纯模型推演"（`--mode=model`，完全离线）失去意义。所以仿真按**同一套口径参数**自建市场，
两侧的规模由参数对齐（`market_stall_count`），而不是靠共享代码。

**取值一律来自 `params.json`**：本模块不写任何数值常量 —— 同一个数值写两遍就是下次漂移的种子。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.streams import StreamSet

#: 品类（名称, 计量单位）。单位影响计价语义，但结构本身写死在代码里是安全的（不是"取值"）
CATEGORIES = (
    ("cat-01", "叶菜", "500g"),
    ("cat-02", "根茎", "500g"),
    ("cat-03", "瓜果", "500g"),
    ("cat-04", "豆制品", "500g"),
)

#: 每个品类下的商品数（结构性常量，非"参数取值"）
PRODUCTS_PER_CATEGORY = 3


@dataclass(frozen=True)
class Stall:
    stall_no: str
    category_id: str


@dataclass(frozen=True)
class Product:
    product_id: str
    category_id: str
    name: str
    base_price_cents: int


@dataclass
class Market:
    """一个市场：摊位、商品与**按营业日给出的价目表**。

    价目表按营业日给（`price_book(day)`），因为主系统的计价依赖"该营业日有生效价目表"
    （无价目表时计价端点直接拒绝）—— 仿真必须复现这条环境事实，否则 `--mode=live` 一开场就全 409。
    """

    stalls: list[Stall]
    products: list[Product]
    _price_books: dict[str, dict[str, int]]

    def stall_nos(self) -> list[str]:
        return [s.stall_no for s in self.stalls]

    def products_of(self, category_id: str) -> list[Product]:
        return [p for p in self.products if p.category_id == category_id]

    def price_book(self, business_date: str) -> dict[str, int]:
        """该营业日的价目表（商品 id → 单价分）。首次访问时生成并缓存，故逐日稳定。"""
        book = self._price_books.get(business_date)
        if book is None:
            book = {p.product_id: p.base_price_cents for p in self.products}
            self._price_books[business_date] = book
        return book


def build_market(params, streams: StreamSet, stall_count: int | None = None) -> Market:
    """按参数建市场：`stall_count` 个摊位（按品类轮转）+ 每品类 `PRODUCTS_PER_CATEGORY` 个商品。

    商品的基准价由 `product_base_price_cents_range` 参数决定，取值来自**该商品自己的流**
    （`purpose="market"`），故"加一个商品"不会平移其他商品的价格。
    """
    count = int(stall_count if stall_count is not None else params.value("market_stall_count"))
    if count < 1:
        raise ValueError(f"摊位数量必须 ≥1，收到 {count}")

    low, high = params.value("product_base_price_cents_range")
    if not (0 < low <= high):
        raise ValueError(f"商品价格区间非法：{params.value('product_base_price_cents_range')}")

    stalls = [Stall(f"A-{i + 1:02d}", CATEGORIES[i % len(CATEGORIES)][0]) for i in range(count)]

    products: list[Product] = []
    for category_id, category_name, _unit in CATEGORIES:
        for index in range(PRODUCTS_PER_CATEGORY):
            product_id = f"{category_id}-p{index + 1}"
            rng = streams.stream(product_id, "price")
            products.append(
                Product(
                    product_id=product_id,
                    category_id=category_id,
                    name=f"{category_name}{index + 1}号",
                    base_price_cents=rng.randint(int(low), int(high)),
                )
            )
    return Market(stalls=stalls, products=products, _price_books={})
