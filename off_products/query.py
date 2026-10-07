"""Structured query spec that an LLM fills in from a natural-language question.

Flow: question -> LLM (tool call with QUERY_SPEC_SCHEMA) -> QuerySpec ->
hard filters (SQL or Pinecone metadata filter) -> semantic ranking on
``semantic_query`` -> LLM writes the answer from the returned records.

Numbers and exclusions are enforced by filters, never left to the embedding.

Examples of questions and the spec they map to:
  "protein bar, at least 20 g protein, no seed oils"
      categories_any=[en:protein-bars],
      nutrients=[{protein_g, serving, >=, 20}], exclude_groups=[seed_oils]
  "potato chips, very few ingredients, avocado oil"
      categories_any=[en:potato-crisps], include_ingredients_all=[en:avocado-oil],
      max_ingredients=4, sort_by=ingredients_n
  "dark chocolate with more than 70% cocoa and under 8 g sugar per serving"
      categories_any=[en:dark-chocolates],
      ingredient_amounts=[{en:cocoa, percent, >, 70}],
      nutrients=[{sugars_g, serving, <, 8}]
  "granola where nuts are the first ingredient, no added sugar"
      first_ingredient_any=[en:nut], exclude_groups=[added_sugars]
"""

from dataclasses import dataclass, field

from .concepts import GROUPS

NUTRIENT_NAMES = [
    "energy_kcal", "protein_g", "fat_g", "saturated_fat_g", "trans_fat_g",
    "carbs_g", "sugars_g", "added_sugars_g", "fiber_g", "salt_g", "sodium_mg",
    "cholesterol_g",
]
AMOUNT_BASES = ["percent", "grams_per_100g", "grams_per_serving"]
_OP_ENUM = [">=", "<=", ">", "<"]

QUERY_SPEC_SCHEMA = {
    "name": "search_products",
    "description": (
        "Search Open Food Facts products. Put every hard requirement in a filter "
        "field; put the remaining descriptive intent (flavour, style, product type) "
        "in semantic_query. Category and ingredient values are "
        "ids such as cat:snack-bars, cat:chips-pretzels (categories) and en:avocado-oil, en:cocoa (ingredients). "
        "Nutrient amounts: use basis 'serving' when the user talks about a bar, bag, "
        "can or serving, and '100g' for densities or percentages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "semantic_query": {"type": "string"},
            "categories_any": {"type": "array", "items": {"type": "string"}},
            "nutrients": {
                "type": "array",
                "description": "Macro/micro nutrient thresholds, e.g. more than 10 g protein.",
                "items": {
                    "type": "object",
                    "properties": {
                        "nutrient": {"enum": NUTRIENT_NAMES},
                        "basis": {"enum": ["serving", "100g"]},
                        "op": {"enum": _OP_ENUM},
                        "value": {"type": "number"},
                    },
                    "required": ["nutrient", "basis", "op", "value"],
                },
            },
            "ingredient_amounts": {
                "type": "array",
                "description": "Thresholds on how much of one ingredient a product contains, "
                               "e.g. at least 30% almonds or 5 g cocoa per serving.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ingredient": {"type": "string"},
                        "basis": {"enum": AMOUNT_BASES},
                        "op": {"enum": _OP_ENUM},
                        "value": {"type": "number"},
                        "declared_only": {
                            "type": "boolean",
                            "description": "Only trust percentages printed on the label, not estimates.",
                        },
                    },
                    "required": ["ingredient", "basis", "op", "value"],
                },
            },
            "include_ingredients_all": {"type": "array", "items": {"type": "string"}},
            "exclude_ingredients": {"type": "array", "items": {"type": "string"}},
            "first_ingredient_any": {"type": "array", "items": {"type": "string"}},
            "exclude_groups": {
                "type": "array",
                "items": {"enum": list(GROUPS)},
                "description": "Product must be known to be free of each group.",
            },
            "include_groups": {"type": "array", "items": {"enum": list(GROUPS)}},
            "max_ingredients": {
                "type": "integer",
                "description": "'minimal ingredients' ~ 5, 'very minimal' ~ 3-4.",
            },
            "labels_all": {"type": "array", "items": {"type": "string"}},
            "exclude_allergens": {"type": "array", "items": {"type": "string"}},
            "countries_any": {"type": "array", "items": {"type": "string"}},
            "max_nova_group": {"type": "integer", "minimum": 1, "maximum": 4},
            "sort_by": {
                "enum": ["relevance", "ingredients_n", "protein_g_serving", "protein_kcal_pct",
                         "sugars_g_serving", "popularity"],
            },
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["semantic_query"],
    },
}


@dataclass
class QuerySpec:
    semantic_query: str = ""
    categories_any: list = field(default_factory=list)
    nutrients: list = field(default_factory=list)
    ingredient_amounts: list = field(default_factory=list)
    include_ingredients_all: list = field(default_factory=list)
    exclude_ingredients: list = field(default_factory=list)
    first_ingredient_any: list = field(default_factory=list)
    exclude_groups: list = field(default_factory=list)
    include_groups: list = field(default_factory=list)
    max_ingredients: int | None = None
    labels_all: list = field(default_factory=list)
    exclude_allergens: list = field(default_factory=list)
    countries_any: list = field(default_factory=list)
    max_nova_group: int | None = None
    sort_by: str = "relevance"
    limit: int = 10

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


_OPS = {">=": "$gte", "<=": "$lte", ">": "$gt", "<": "$lt"}
_SORT_SQL = {
    "ingredients_n": "p.ingredients_n ASC",
    "protein_g_serving": "p.protein_g_serving DESC",
    "protein_kcal_pct": "p.protein_kcal_pct DESC",
    "sugars_g_serving": "p.sugars_g_serving ASC",
    "popularity": "p.unique_scans_n DESC",
}


def _check_op(op):
    if op not in _OPS:
        raise ValueError(f"unknown operator {op}")


def _nutrient_column(c):
    if c["nutrient"] not in NUTRIENT_NAMES or c["basis"] not in ("serving", "100g"):
        raise ValueError(f"unknown nutrient constraint {c}")
    _check_op(c["op"])
    return f"{c['nutrient']}_{c['basis']}"


def _check_groups(groups):
    unknown = set(groups) - set(GROUPS)
    if unknown:
        raise ValueError(f"unknown ingredient groups {sorted(unknown)}")


def build_where(spec: QuerySpec, ph="?"):
    """Return (where_clauses, params) over the ``products p`` / ``product_tags`` /
    ``product_ingredients`` schema. ``ph`` is the driver's placeholder
    ("?" for sqlite3, "%s" for psycopg).

    Rows missing a constrained value are excluded: a product with unknown
    protein can't be said to have >= 20 g.
    """
    where, params = ["p.obsolete = 0"], []

    def has_tag(kind, tags, negate=False, all_=False):
        if not tags:
            return
        q = (f"SELECT 1 FROM product_tags t WHERE t.code = p.code AND t.kind = {ph} "
             "AND t.tag IN ({})")
        if all_:
            for tag in tags:
                where.append(f"EXISTS ({q.format(ph)})")
                params.extend([kind, tag])
            return
        where.append(("NOT " if negate else "") + f"EXISTS ({q.format(','.join([ph] * len(tags)))})")
        params.extend([kind, *tags])

    _check_groups(spec.exclude_groups + spec.include_groups)
    has_tag("category", spec.categories_any)
    has_tag("ingredient", spec.include_ingredients_all, all_=True)
    has_tag("ingredient", spec.exclude_ingredients, negate=True)
    has_tag("free_of", spec.exclude_groups, all_=True)
    has_tag("contains_group", spec.include_groups, all_=True)
    has_tag("label", spec.labels_all, all_=True)
    has_tag("allergen", spec.exclude_allergens, negate=True)
    has_tag("country", spec.countries_any)
    for c in spec.nutrients:
        where.append(f"p.{_nutrient_column(c)} {c['op']} {ph}")
        params.append(c["value"])
    for a in spec.ingredient_amounts:
        if a["basis"] not in AMOUNT_BASES:
            raise ValueError(f"unknown amount basis {a['basis']}")
        _check_op(a["op"])
        declared = " AND i.declared = 1" if a.get("declared_only") else ""
        where.append(
            "EXISTS (SELECT 1 FROM product_ingredients i WHERE i.code = p.code "
            f"AND i.ingredient = {ph} AND i.{a['basis']} {a['op']} {ph}{declared})"
        )
        params.extend([a["ingredient"], a["value"]])
    if spec.first_ingredient_any:
        where.append(f"p.first_ingredient IN ({','.join([ph] * len(spec.first_ingredient_any))})")
        params.extend(spec.first_ingredient_any)
    if spec.max_ingredients is not None:
        where.append(f"p.ingredients_n <= {ph}")
        params.append(spec.max_ingredients)
    if spec.max_nova_group is not None:
        where.append(f"p.nova_group <= {ph}")
        params.append(spec.max_nova_group)
    return where, params


def order_by(spec: QuerySpec):
    """Explicit sort first; callers append similarity and popularity tie-breaks."""
    return [_SORT_SQL[spec.sort_by]] if spec.sort_by in _SORT_SQL else []


def to_sql(spec: QuerySpec):
    """Compile to SQLite against the schema written by store.py. Returns (sql, params)."""
    where, params = build_where(spec)
    order = order_by(spec) + ["p.unique_scans_n DESC"]
    sql = (
        "SELECT p.code, p.name, p.brand, p.record FROM products p WHERE "
        + " AND ".join(where)
        + " ORDER BY " + ", ".join(order) + " LIMIT ?"
    )
    params.append(spec.limit)
    return sql, params


def to_pinecone_filter(spec: QuerySpec):
    """Compile to a Pinecone metadata filter over ``normalize.to_pinecone_metadata``.

    ``ingredient_amounts`` is nested data that flat metadata can't express;
    over-fetch from Pinecone and apply ``matches_ingredient_amounts`` afterwards.
    """
    _check_groups(spec.exclude_groups + spec.include_groups)
    clauses = []
    if spec.categories_any:
        clauses.append({"category_tags": {"$in": spec.categories_any}})
    for tag in spec.include_ingredients_all:
        clauses.append({"ingredient_tags": {"$in": [tag]}})
    if spec.exclude_ingredients:
        clauses.append({"ingredient_tags": {"$nin": spec.exclude_ingredients}})
    for group in spec.exclude_groups:
        clauses.append({"free_of": {"$in": [group]}})
    for group in spec.include_groups:
        clauses.append({"contains_groups": {"$in": [group]}})
    if spec.first_ingredient_any:
        clauses.append({"first_ingredient": {"$in": spec.first_ingredient_any}})
    for tag in spec.labels_all:
        clauses.append({"label_tags": {"$in": [tag]}})
    if spec.exclude_allergens:
        clauses.append({"allergen_tags": {"$nin": spec.exclude_allergens}})
    if spec.countries_any:
        clauses.append({"country_tags": {"$in": spec.countries_any}})
    for c in spec.nutrients:
        clauses.append({_nutrient_column(c): {_OPS[c["op"]]: c["value"]}})
    if spec.max_ingredients is not None:
        clauses.append({"ingredients_n": {"$lte": spec.max_ingredients}})
    if spec.max_nova_group is not None:
        clauses.append({"nova_group": {"$lte": spec.max_nova_group}})
    return {"$and": clauses} if clauses else {}


def matches_ingredient_amounts(record, spec: QuerySpec):
    """Post-filter a canonical record on ``spec.ingredient_amounts``."""
    from .normalize import ingredient_amounts

    rows = {r["ingredient"]: r for r in ingredient_amounts(record)}
    compare = {">=": float.__ge__, "<=": float.__le__, ">": float.__gt__, "<": float.__lt__}
    for a in spec.ingredient_amounts:
        row = rows.get(a["ingredient"])
        value = row and row[a["basis"]]
        if value is None or (a.get("declared_only") and not row["declared"]):
            return False
        if not compare[a["op"]](float(value), float(a["value"])):
            return False
    return True
