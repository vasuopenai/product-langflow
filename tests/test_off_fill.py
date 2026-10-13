"""off_fill: Open Food Facts fills only what USDA leaves empty, and only when it stays plausible."""

from off_products import off_fill, usda

RAW = {"fdc_id": "1", "gtin_upc": "00012345678905", "description": "GRANOLA", "brand_name": "B",
       "ingredients": "OATS, HONEY, ALMONDS", "serving_size": "40", "serving_size_unit": "g",
       "nutrients": {"1008": 450, "1003": 10, "1004": 18, "1005": 60, "2000": 20, "1093": 120}}


def test_off_nutrients_convert_to_usda_units():
    got = off_fill._nutrients([{"name": "fiber", "100g": 6.5}, {"name": "sodium", "100g": 0.12},
                               {"name": "saturated-fat", "100g": 2.0}, {"name": "iron", "100g": 0.004},
                               {"name": "sugars", "100g": -1}])
    assert got == {"1079": 6.5, "1093": 120.0, "1258": 2.0}  # sodium g -> mg; unknown and negative dropped
    assert off_fill._ingredients_text([{"lang": "fr", "text": "avoine"}, {"lang": "en", "text": "oats"}]) == "oats"


def test_fill_only_what_usda_lacks():
    entry = {"ingredients_text": "oats, honey", "nutrients": {"1079": 6.5, "1258": 2.0, "1003": 99}}
    fill = off_fill.fill_for(RAW, entry)
    assert fill == {"nutrients": {"1079": 6.5, "1258": 2.0}}  # protein and ingredients come from USDA
    record, filled = off_fill.refill(RAW, entry)
    assert filled == ["fiber_g", "saturated_fat_g"]
    assert record["nutrition"]["per_100g"]["fiber_g"] == 6.5 and record["nutrition"]["per_100g"]["protein_g"] == 10
    assert record["source"]["filled_from_off"] == filled
    assert "fill" not in (record["source"]["raw"].get("off") or {}) or record["source"]["raw"]["off"]["fill"]
    # The stored raw keeps USDA's own nutrients; the fill lives in its "off" block.
    assert "1079" not in record["source"]["raw"]["nutrients"]
    again = usda.normalize(record["source"]["raw"])  # re-deriving keeps the fill
    assert again["nutrition"]["per_100g"]["fiber_g"] == 6.5


def test_empty_usda_ingredients_are_filled():
    raw = {**RAW, "ingredients": ""}
    record, filled = off_fill.refill(raw, {"ingredients_text": "Oats, honey, almonds", "nutrients": {}})
    assert filled == ["ingredients"] and record["ingredients"]["items"]


def test_fill_that_makes_a_product_implausible_is_refused():
    record, reason = off_fill.refill(RAW, {"nutrients": {"1258": 90}})  # saturated fat above total fat
    assert record is None and reason == "would be inconsistent"
    record, reason = off_fill.refill(RAW, {"nutrients": {"1079": 150}})  # 150 g fiber per 100 g
    assert record is None and reason in ("would be implausible", "would be inconsistent")
    assert off_fill.refill(RAW, None) == (None, "nothing to fill")
