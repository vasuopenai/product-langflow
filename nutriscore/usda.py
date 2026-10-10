"""Our product record (off_products.usda.normalize) -> Nutri-Score 2023 inputs.

What this module decides, because USDA does not say it directly:

- which variant of the algorithm applies (general food, cheese, red meat,
  fats/oils/nuts/seeds, beverage, water), from USDA's branded_food_category
  (``record["main_category"]``), the product name and the ingredient list;
- whether the Nutri-Score applies at all (alcohol, baby food, supplements,
  herbs/spices, salt, vinegar, chewing gum, sugar substitutes) and whether the
  label values are "as sold" for a product that is eaten prepared (drink mixes,
  cake mixes, ground coffee), in which case Open Food Facts refuses to compute and
  so do we;
- the % of fruit, vegetables and legumes, estimated from the ingredient list
  (USDA never declares it);
- presence of non-nutritive sweeteners (beverages) and the red-meat share.

All of it is rules on text; see docs/NUTRISCORE.md for the mapping and its limits.
"""

import re

from . import algorithm as alg

VERSION = "2023"
KCAL_TO_KJ = 4.184


def _norm(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


# --- out of scope ---------------------------------------------------------------

# Normalized USDA category -> reason. Nutri-Score does not apply (EU regulation
# 1169/2011 Annex V exemptions and the Nutri-Score regulation of use; Open Food
# Facts' categories_exempted_from_nutriscore).
NOT_APPLICABLE_CATEGORIES = [
    (r"^alcohol|^alcoholic beverages|^beer$", "alcoholic beverage"),
    (r"baby|infant", "baby food / infant formula"),
    (r"supplement|vitamin|^minerals$|health care|natural remedies|^weight control$|"
     r"oral hygiene|^media$|^gardening$", "dietary supplement, meal replacement or non-food"),
    (r"^herbs (?:and )?spices$|^herbs spices extracts$", "herbs and spices"),
    (r"^vinegars", "vinegar"),
]
_NA_CATEGORIES = [(re.compile(rx), why) for rx, why in NOT_APPLICABLE_CATEGORIES]
_ALCOHOL_NAME = re.compile(r"\bhard (?:cider|seltzer|lemonade|tea|kombucha)\b|\bbeer\b|\bwine\b|\bvodka\b|"
                           r"\bwhiske?y\b|\btequila\b|\bliqueur\b|\bsake\b|\blager\b|\bale\b|\bipa\b|"
                           r"\bbourbon\b|\bgin\b|\bmezcal\b|\bmead\b")
_NOT_ALCOHOL_NAME = re.compile(r"\b(?:root|ginger|birch) beer\b|\bginger ale\b|non ?alcoholic|alcohol free|"
                               r"\b0 0\b|\bvinegar\b|\bsparkling juice\b")
_SWEETENER_PRODUCT = re.compile(r"sweetener|stevia|sucralose|monk ?fruit|erythritol|allulose|xylitol|splenda|"
                                r"truvia|aspartame|saccharin")
_SUGAR_CATEGORY = re.compile(r"sugar|sweet")

# Label values are for the product as sold, but it is eaten prepared: Open Food Facts
# requires prepared-product values for these (has_category_that_should_have_prepared_
# nutrition_data: beverage preparations, dessert mixes, dried products to be
# rehydrated, flavoured syrups, cocoa/chocolate powders) and USDA has none.
NEEDS_PREPARED_CATEGORIES = re.compile(r"powdered drinks|drinks powdered|liquid water enhancer|not ready to drink")
# Categories that are mostly mixes but also hold ready products: prepared only if named a mix.
MIX_CATEGORIES = re.compile(r"cake cookie cupcake mixes|^gravy mix$")
COFFEE_CATEGORIES = re.compile(r"^coffee$|coffee tea substitutes|coffee coffee substitutes|coffee instant")
_MIX_NAME = re.compile(r"\b(?:drink|beverage|shake|smoothie|cocoa|hot chocolate|cappuccino|latte|lemonade|"
                       r"tea|coffee|soup|gravy|pudding|gelatin|cake|brownie|cupcake|muffin|cookie|frosting|"
                       r"dessert)\b.*\bmix(?:es)?\b|\bpowder\b|(?<!from )\bconcentrate\b")
_RTD_COFFEE = re.compile(r"\b(?:drink|beverage|cold brew|ready to drink|bottle|latte|frappuccino|"
                         r"iced coffee|nitro|fl oz|iced|mocha|macchiato)\b")
_COFFEE_DRY = re.compile(r"\b(?:ground|whole bean|beans|pods?|k cups?|instant|mix|concentrate|roast|"
                         r"capsules?|singles|brew bags?|filter packs?)\b")

# --- beverages and water -------------------------------------------------------

BEVERAGE_CATEGORIES = re.compile(
    r"^soda$|juice|nectar|iced bottle tea|sport drinks|energy protein muscle recovery drinks|"
    r"^other drinks$|non alcoholic beverages ready to drink|drinks flavoured|drinks soft drinks|"
    r"drinks energy drinks|^ready to drink$|plant based milk|plant based water|^milk$|"
    r"milk milk substitutes|breakfast drinks|^tea bags$|tea bags loose|^water$|^coffee$|"
    r"coffee tea substitutes|coffee coffee substitutes")
_NOT_A_DRINK = re.compile(r"\bpowder(?:ed)?\b|\bdry milk\b|\bcondensed\b|\bevaporated\b|(?<!from )\bconcentrate\b|"
                          r"\bfrozen\b|\bpopsicles?\b|\bbars?\b|\bgumm(?:y|ies)\b|\bjell?y\b")
# USDA's milk categories also hold yogurts, cheeses, creamers and even boxed mac & cheese.
_NOT_A_MILK_DRINK = re.compile(r"yogh?urt|cheese|cottage|pudding|\bmac\b|macaroni|ice cream|butter\b|"
                               r"sour cream|\bdips?\b|whipped|\bcurd\b|custard|mousse|dessert|creamer|"
                               r"half (?:and )?half|\bghee\b|\bcream\b(?! soda)|\bskyr\b|\bquark\b|pouch")
_DRINKABLE_DAIRY = re.compile(r"\bkefir\b|\bdrinkable\b|\byogh?urt drink\b|\bsmoothie\b|\blassi\b|"
                              r"\bdairy drink\b|\bdrinking yogh?urt\b")
_FLAVORED = re.compile(r"\b(?:flavou?r|essence|infused|lemon|lime|\w*berry|\w*berries|orange|grapefruit|mango|"
                       r"peach|cherry|apple|grape|\w*melon|cucumber|mint|tea|coffee|energy|vitamin|sweet|juice|"
                       r"coconut|tangerine|pineapple|passion|pomegranate|citrus|kiwi|guava|hibiscus|"
                       r"ginger|botanical|zero sugar|cola|tonic|protein|collagen|cbd|caffeine)")
_WATER_INGREDIENT = re.compile(
    r"^(?:[a-z]+\s+){0,4}water$|^carbonation$|^carbon dioxide$|^(?:trace\s+)?minerals?(?: for taste)?$|"
    r"^electrolytes?(?: for taste)?$|^(?:calcium|magnesium|potassium|sodium|zinc)\s+"
    r"(?:chloride|sulfate|bicarbonate|carbonate|citrate|lactate|hydroxide|phosphate)$|^salt$|^sea salt$")

# --- cheese, fats/oils/nuts/seeds, red meat --------------------------------------

CHEESE_CATEGORIES = re.compile(r"^cheese|processed cheese")
_NOT_CHEESE = re.compile(r"\bvegan\b|plant based|dairy free|non dairy|\bcheesecake\b|\bcheese (?:dip|sauce)\b")

FAT_CATEGORIES = re.compile(r"\boils?\b|fats edible|cooking oils and fats|^butter|margarine|"
                            r"^cream$|^cream cream substitutes$|nut seed butters")
_NOT_FAT = re.compile(r"\bapple butter\b|\bcookie butter\b|\bchocolate\b|\bcocoa\b|hazelnut spread|"
                      r"\bfruit butter\b|\bpumpkin butter\b|\bcinnamon\b.*\bspread\b|\bmilk\b|\bvinegar\b|"
                      r"\bdressing\b|\bmarinade\b")
# Nuts and seeds (HS 0801, 0802, 1202, 1204, 1206, 1207, 2008.11/19), by product name,
# in the USDA categories where they are sold. Chestnuts are excluded by the rule.
NUT_SEED_CATEGORIES = re.compile(r"popcorn peanuts seeds|other grains seeds|nuts seeds|snack foods nuts|"
                                 r"wholesome snacks|baking decorations|pre packaged fruit vegetables|"
                                 r"fruit nuts seeds")
_NUT_SEED = re.compile(
    r"\b(?:almonds?|cashews?|peanuts?|pecans?|pistachios?|walnuts?|macadamias?|hazelnuts?|filberts?|"
    r"brazil nuts?|pine nuts?|mixed nuts|nuts|sunflower (?:seeds?|kernels|nuts)|pumpkin seeds|pepitas|"
    r"chia(?: seeds?)?|flax ?seeds?|flaxseed|flax meal|sesame seeds?|hemp (?:seeds?|hearts)|seeds|nut pieces)\b")
_NOT_NUT_SEED = re.compile(r"popcorn|chocolate|candy|caramel|toffee|brittle|cluster|trail|\bmix\b|\bbars?\b|"
                           r"cookie|cracker|coated|covered|yogh?urt|granola|chestnut|coconut|crunch|bites|"
                           r"butter|cereal|snack mix|clusters|dried fruit|raisin|cranberr|\bmilk\b|"
                           r"\bpie\b|\bpraline")

_RED_MEAT = re.compile(r"\b(?:beef|pork|veal|lamb|mutton|goat|venison|bison|buffalo|elk|boar|ham|bacon|"
                       r"prosciutto|pancetta|guanciale|chorizo|salami|pepperoni|sirloin|brisket|"
                       r"corned beef|pastrami|hot dogs?|frankfurters?|bratwurst|kielbasa)\b")
# A red-meat word directly qualified by one of these is not meat ("beef broth",
# "natural pork flavor", "turkey ham"); "beef and natural flavorings" still is.
_RED_MEAT_WORD = _RED_MEAT.pattern[5:-3]
_NOT_RED_MEAT = re.compile(
    r"\b(?:flavou?r(?:ed|ing)?|style|imitation|turkey|chicken)\s+(?:\w+\s+)?(?:" + _RED_MEAT_WORD + r")\b|"
    r"\b(?:" + _RED_MEAT_WORD + r")\s+(?:\w+\s+)?(?:broth|stock|fat|lard|tallow|gelatin|collagen|casings?|"
    r"flavou?r\w*|extract|powder|bouillon|base|seasoning|concentrate|juice|drippings?|cracklings?|rinds?|"
    r"skins?)\b|plant based|meatless|vegan")
MEAT_CATEGORIES = re.compile(r"meat|sausage|hotdog|bacon|pepperoni|salami|cold cuts|patties|burgers|"
                             r"^beef|^pork|^ham|smallgoods|fresh meat")

# --- fruit, vegetables and legumes ---------------------------------------------

_FVL = re.compile(
    r"\b(?:"
    # fruit
    r"apples?|applesauce|apricots?|avocados?|bananas?|blackberr\w*|blueberr\w*|boysenberr\w*|cantaloupes?|"
    r"cherr(?:y|ies)|cranberr\w*|currants?|dates|elderberr\w*|figs?|goji|gooseberr\w*|grapes?|grapefruits?|"
    r"guavas?|honeydew|kiwis?|kiwifruit|lemons?|limes?|lychees?|mangos?|mangoes|melons?|nectarines?|"
    r"oranges?|papayas?|passion ?fruit|peach(?:es)?|pears?|persimmons?|pineapples?|plums?|pomegranates?|"
    r"prunes?|quinces?|raisins?|raspberr\w*|strawberr\w*|tangerines?|clementines?|mandarins?|watermelons?|"
    r"a[cç]a[ií]|olives?|jackfruit|dragon ?fruit|pitaya|plantains?|berr(?:y|ies)|fruits?|"
    # vegetables
    r"artichokes?|arugula|asparagus|beets?|beetroots?|broccoli|brussels sprouts|cabbages?|carrots?|"
    r"cauliflower|celery|chard|chives?|collards?|cucumbers?|eggplants?|endives?|fennel|garlic|"
    r"green beans|kale|kohlrabi|leeks?|lettuce|mushrooms?|okra|onions?|parsnips?|peas|pumpkins?|"
    r"radish(?:es)?|romaine|rutabagas?|scallions?|shallots?|spinach|squash|zucchinis?|sweet corn|"
    r"corn kernels|tomato(?:es)?|turnips?|watercress|water chestnuts?|bamboo shoots|bean sprouts|"
    r"seaweed|kelp|nori|vegetables?|veggies|greens|sauerkraut|kimchi|pickles?|cilantro|parsley|basil|"
    r"dill|jalape\w+|poblanos?|pimientos?|pimentos?|peppers|(?:bell|sweet|green|red|yellow|orange|banana|"
    r"cherry|roasted|chipotle|habanero|serrano|anaheim|chil[ei]) peppers?|"
    # legumes
    r"beans?|chick ?peas?|garbanzos?|lentils?|split peas|edamame|soybeans?|soy beans?|black eyed peas"
    r")\b")
_NOT_FVL = re.compile(
    r"powder|flour|starch|protein|isolate|fiber|fibre|extract|\boils?\b|flavou?r|colou?r|sugar(?! snap)|syrup|"
    r"pectin|solids|seasoning|spices?\b|vinegar|lecithin|\bgum\b|candied|freeze[- ]?dried|seeds?\b|sauce|"
    r"ketchup|soup|salsa|dressing|relish|chips|crisps|bread|pasta|noodles?|crackers?|jelly beans?|vanilla|"
    r"cocoa|cacao|coffee|carob|monk fruit|wine|potato|yams?\b|peanut|coconut|black pepper|white pepper|"
    r"cayenne|crushed red|ground red|pepper flakes|(?<!sweet )\bcorn\b(?! kernels)|grain|\bbutter\b|\bcake\b|"
    r"glycer|shortening|\bmono\b|lactic|cellulose|enzyme|rennet|margarine|\bfat\b|"
    r"\bpie\b|\bfilling\b|\bjuice concentrate for\b|\bfruit snacks?\b|\bmilk\b|cultures?|\bmash\b")
_FVL_OILS = re.compile(r"\b(?:olive|avocado) oil\b")  # fats/oils category only (2023 rule)

# OFF's fallback when there is no ingredient list (Food.pm,
# fruits_vegetables_legumes_by_category_if_no_ingredients_specified_sorted).
FVL_BY_CATEGORY = [
    (re.compile(r"french fries potatoes"), 0),
    (re.compile(r"juice"), 100),
    (re.compile(r"pre packaged fruit vegetables|frozen vegetables|canned vegetables|^tomatoes$|"
                r"canned fruit|frozen fruit|vegetables prepared|fruits? unprepared|vegetables unprepared|"
                r"canned bottled beans|vegetable and lentil"), 90),
    (re.compile(r"\bjam\b"), 50),
]
# Canned plant foods whose packing water is not eaten (OFF:
# en:canned-plant-based-foods but not en:canned-fruits): % recomputed without water.
DRAINED_CATEGORIES = re.compile(r"canned vegetables|canned bottled beans|^tomatoes$")

# --- non-nutritive sweeteners ---------------------------------------------------

# The report's list of non-nutritive sweeteners (EU E950-E969 minus polyols):
# acesulfame K, aspartame, cyclamates, saccharin, sucralose, thaumatin,
# neohesperidine DC, steviol glycosides, neotame, advantame, salt of aspartame-
# acesulfame. Polyols (erythritol etc.) are not counted. Our ingredient group
# "artificial_sweeteners" covers the synthetic ones; stevia, monk fruit and
# thaumatin are matched here.
_NNS = re.compile(r"stevia|steviol|rebaudioside|\breb [am]\b|monk ?fruit|luo han guo|thaumatin|"
                  r"neohesperidin|sucralose|aspartame|acesulfame|saccharin|neotame|advantame|cyclamate")


# --- helpers -------------------------------------------------------------------

def _children(items, i):
    out, d = [], items[i]["depth"]
    for j in range(i + 1, len(items)):
        if items[j]["depth"] <= d:
            break
        if items[j]["depth"] == d + 1:
            out.append(j)
    return out


def _percent(item):
    return item["percent"] if item.get("percent") is not None else item.get("percent_estimate")


_SALT = re.compile(r"^(?:\w+\s+)?(?:sea\s+|kosher\s+|iodized\s+|himalayan\s+|pink\s+)?salt$")


def reestimate(items, salt_g=None):
    """Copy of ``items`` with one correction to the label-order percent estimate of
    off_products.usda: salt near the end of a short list gets shares like 10-25%,
    while the nutrition panel says how much salt there is. Salt is capped at the
    product's salt content (2% when unknown) and the freed share goes to the other
    undeclared ingredients of the same level, pro rata. (Also capping acids, flavours
    and spices was tried and did not improve agreement with Open Food Facts.)"""
    items = [dict(it) for it in items]

    def level(indices, total):
        if not indices or total is None:
            return
        fixed = sum(items[i]["percent"] for i in indices if items[i].get("percent") is not None)
        free, capped = [], 0.0
        for i in indices:
            it = items[i]
            if it.get("percent") is not None or it.get("percent_estimate") is None:
                continue
            if it.get("minor"):  # already estimated within its "2% or less" cap
                capped += it["percent_estimate"]
            elif _SALT.match((it["text"] or "").strip()):
                it["percent_estimate"] = min(it["percent_estimate"], salt_g if salt_g is not None else 2.0)
                capped += it["percent_estimate"]
            else:
                free.append(i)
        left = total - fixed - capped
        old = sum(items[i]["percent_estimate"] for i in free)
        if free and old > 0 and left > 0:
            for i in free:
                items[i]["percent_estimate"] = items[i]["percent_estimate"] * left / old
        for i in indices:
            level(_children(items, i), _percent(items[i]))

    level([i for i, it in enumerate(items) if it["depth"] == 0], 100.0)
    return items


def matching_percent(items, match):
    """Share of the product (in %) made of ingredients for which ``match(text)`` is
    true, as Open Food Facts' add_ingredients_matching_function does it: a matching
    ingredient counts with its (declared or estimated) percent; a non-matching one is
    searched for matching sub-ingredients. Returns (percent, water_percent)."""
    def walk(indices):
        pct = water = 0.0
        for i in indices:
            it = items[i]
            p = _percent(it)
            kids = _children(items, i)
            sub_water = 0.0
            if match(it["text"]) and p is not None:
                pct += p
            elif kids:
                sub, sub_water = walk(kids)
                pct += sub
            if (it.get("id") == "en:water" or re.fullmatch(r"(?:\w+ )?water", it["text"] or "")) and p is not None:
                water += p
            else:
                water += sub_water
        return pct, water

    return walk([i for i, it in enumerate(items) if it["depth"] == 0])


def is_fvl(text, fats=False):
    text = (text or "").lower()
    if fats and _FVL_OILS.search(text):
        return True
    return bool(_FVL.search(text)) and not _NOT_FVL.search(text)


def is_red_meat(text):
    text = (text or "").lower()
    return bool(_RED_MEAT.search(text)) and not _NOT_RED_MEAT.search(text)


def _ingredient_texts(record):
    return [it["text"] or "" for it in (record.get("ingredients") or {}).get("items") or []]


def _top_texts(record):
    return [it["text"] or "" for it in (record.get("ingredients") or {}).get("items") or [] if it["depth"] == 0]


def _alcohol_g(record):
    raw = ((record.get("source") or {}).get("raw") or {}).get("nutrients") or {}
    v = raw.get("1018")
    return float(v) if isinstance(v, (int, float)) else None


# --- decisions -------------------------------------------------------------------

def scope(record):
    """None if the Nutri-Score applies, else (status, reason) with status
    "not_applicable" or "needs_prepared_values"."""
    cat = _norm(record.get("main_category"))
    name = _norm(record.get("name"))
    n100 = (record.get("nutrition") or {}).get("per_100g") or {}
    for rx, why in _NA_CATEGORIES:
        # USDA files broths under "Herbs/Spices/Extracts"
        if rx.search(cat) and not (why == "herbs and spices" and re.search(r"broth|stock|bouillon|soup", name)):
            return "not_applicable", why
    if (_alcohol_g(record) or 0) > 1.2:
        return "not_applicable", "alcoholic beverage (>1.2% alcohol)"
    if _beverage_category(cat, name) and _ALCOHOL_NAME.search(name) and not _NOT_ALCOHOL_NAME.search(name):
        return "not_applicable", "alcoholic beverage"
    if "gum" in cat and re.search(r"\bgum\b", name):
        return "not_applicable", "chewing gum"
    head = _norm((record.get("name") or "").split(",")[0])  # "Apple Cider Vinegar, Apple Cider"
    if head.endswith("vinegar"):  # not "Salt And Vinegar Crackers" or "Vinegar Ketchup"
        return "not_applicable", "vinegar"
    salt = n100.get("salt_g")
    if re.search(r"\bsalt\b", name) and salt is not None and salt >= 50:
        return "not_applicable", "salt"
    if _SUGAR_CATEGORY.search(cat) and _SWEETENER_PRODUCT.search(name):
        return "not_applicable", "sugar substitute"
    if NEEDS_PREPARED_CATEGORIES.search(cat) or (MIX_CATEGORIES.search(cat) and re.search(r"\bmix", name)):
        return "needs_prepared_values", f"category {record.get('main_category')!r} is eaten prepared"
    if COFFEE_CATEGORIES.search(cat) and not (_RTD_COFFEE.search(name) and not _COFFEE_DRY.search(name)) \
            and not re.search(r"\btea\b", name):
        return "needs_prepared_values", "coffee as sold (ground, pods, instant)"
    if _MIX_NAME.search(name) and (_beverage_category(cat, name) or "mix" in name.split()
                                   or re.search(r"pudding|gelatin|cocoa|drink", cat)):
        return "needs_prepared_values", "mix, powder or concentrate sold before preparation"
    return None


def variant(record):
    """(variant, rule) for an in-scope product; variant in algorithm.VARIANTS."""
    cat = _norm(record.get("main_category"))
    name = _norm(record.get("name"))
    if _is_plain_water(record, cat, name):
        return alg.WATER, "plain water"
    if re.search(r"yogh?urt|dairy|milk", cat) and _DRINKABLE_DAIRY.search(name):
        return alg.BEVERAGE, "drinkable dairy"
    if _beverage_category(cat, name) and not _NOT_A_DRINK.search(name) and not (
            re.search(r"\bmilk\b", cat) and _NOT_A_MILK_DRINK.search(name)):
        return alg.BEVERAGE, f"beverage category {record.get('main_category')!r}"
    if CHEESE_CATEGORIES.search(cat) and not _NOT_CHEESE.search(name):
        return alg.CHEESE, "cheese category"
    if FAT_CATEGORIES.search(cat) and not _NOT_FAT.search(name):
        return alg.FATS, f"fats/oils category {record.get('main_category')!r}"
    if NUT_SEED_CATEGORIES.search(cat) and _NUT_SEED.search(name) and not _NOT_NUT_SEED.search(name):
        top = _top_texts(record)
        if not top or _NUT_SEED.search(top[0]) or re.search(r"almond|cashew|peanut|pecan|pistachio|walnut|"
                                                             r"macadamia|hazelnut|filbert|brazil|sunflower|"
                                                             r"pumpkin|pepita|chia|flax|sesame|hemp", top[0]):
            return alg.FATS, "nuts/seeds by name"
    return alg.GENERAL, "general food"


def _beverage_category(cat, name):
    if cat.startswith("frozen fruit"):  # "Frozen Fruit & Fruit Juice Concentrates": mostly fruit
        return bool(re.search(r"juice|lemonade|limeade|drink|punch|smoothie", name))
    return bool(BEVERAGE_CATEGORIES.search(cat))


def _is_plain_water(record, cat, name):
    if not re.search(r"^water$|plant based water|non alcoholic beverages ready to drink|other drinks", cat):
        return False
    if cat != "water" and not re.search(r"\bwater\b", name):
        return False
    if _FLAVORED.search(name):
        return False
    n100 = (record.get("nutrition") or {}).get("per_100g") or {}
    if (n100.get("energy_kcal") or 0) > 1 or (n100.get("sugars_g") or 0) > 0:
        return False
    texts = _ingredient_texts(record)
    return all(_WATER_INGREDIENT.search(t.strip()) and not _FLAVORED.search(t) for t in texts)


def _items(record):
    items = (record.get("ingredients") or {}).get("items") or []
    salt = ((record.get("nutrition") or {}).get("per_100g") or {}).get("salt_g")
    return reestimate(items, salt) if items else []


def red_meat_percent(record):
    items = _items(record)
    pct = matching_percent(items, is_red_meat)[0] if items else None
    # Plain cuts and patties: OFF uses the category's customs heading (HS 02.01-02.06,
    # fresh/frozen red meat); we use a meat category plus a red-meat product name.
    cat = _norm(record.get("main_category"))
    if (pct is None or pct <= 10) and MEAT_CATEGORIES.search(cat) and is_red_meat(record.get("name")):
        return 100.0
    return pct


def fvl_percent(record, fats=False):
    """(percent, source) with source "ingredients", "category" or None."""
    items = _items(record)
    cat = _norm(record.get("main_category"))
    if items:
        pct, water = matching_percent(items, lambda t: is_fvl(t, fats))
        if DRAINED_CATEGORIES.search(cat) and 0 < water < 100:
            pct = pct * 100 / (100 - water)
        return round(min(pct, 100.0), 1), "ingredients"
    for rx, value in FVL_BY_CATEGORY:
        if rx.search(cat):
            return float(value), "category"
    return None, None


def non_nutritive_sweeteners(record):
    groups = (record.get("derived") or {}).get("groups") or {}
    if (groups.get("artificial_sweeteners") or {}).get("status") == "contains":
        return True
    return any(_NNS.search(t) for t in _ingredient_texts(record))


# --- entry point -------------------------------------------------------------------

REQUIRED = ["energy_kcal", "fat_g", "saturated_fat_g", "sugars_g", "salt_g", "protein_g"]


def compute(record):
    """Nutri-Score 2023 for one product record. Never raises on missing data:
    grade is None and ``reasons`` says why when it cannot be computed."""
    out = {"score": None, "grade": None, "category_variant": None, "variant_rule": None,
           "components": {}, "version": VERSION, "missing": [], "estimated": [], "reasons": []}
    out_of_scope = scope(record)
    if out_of_scope:
        out["status"], reason = out_of_scope
        out["reasons"].append(reason)
        return out

    var, rule = variant(record)
    out["category_variant"], out["variant_rule"] = var, rule
    out["estimated"].append("category_variant")
    if var == alg.WATER:
        res = alg.score(alg.WATER)
        out.update(score=res["score"], grade=res["grade"], status="computed")
        return out

    n100 = dict((record.get("nutrition") or {}).get("per_100g") or {})
    if n100.get("salt_g") is None and n100.get("sodium_mg") is not None:
        n100["salt_g"] = n100["sodium_mg"] * 2.5 / 1000
    # OFF's two deductions: no fat -> no saturated fat; no carbohydrates -> no sugars.
    if n100.get("saturated_fat_g") is None and n100.get("fat_g") == 0:
        n100["saturated_fat_g"] = 0.0
    if n100.get("sugars_g") is None and n100.get("carbs_g") == 0:
        n100["sugars_g"] = 0.0
    missing = [k for k in REQUIRED if n100.get(k) is None]
    if n100.get("fiber_g") is None:
        out["missing"].append("fiber_g")  # scored as 0 points, like OFF
    if missing:
        out["missing"] = missing + out["missing"]
        out["status"] = "missing_data"
        if (record.get("nutrition") or {}).get("implausible"):
            out["reasons"].append("nutrition values failed plausibility checks")
        out["reasons"].append("missing " + ", ".join(missing))
        return out

    fats = var == alg.FATS
    fvl, fvl_source = fvl_percent(record, fats)
    if fvl_source:
        out["estimated"].append(f"fruits_vegetables_legumes ({fvl_source})")
    red = None
    if var == alg.GENERAL:
        red = red_meat_percent(record)
        if red is not None and red > 10:
            var = alg.RED_MEAT
            out["category_variant"], out["variant_rule"] = var, f"red meat ~{red:.0f}% of ingredients"
    nns = non_nutritive_sweeteners(record) if var == alg.BEVERAGE else False

    res = alg.score(
        var,
        energy_kj=n100["energy_kcal"] * KCAL_TO_KJ,
        sugars_g=n100["sugars_g"],
        saturated_fat_g=n100["saturated_fat_g"],
        fat_g=n100["fat_g"],
        salt_g=n100["salt_g"],
        fiber_g=n100.get("fiber_g"),
        proteins_g=n100["protein_g"],
        fruits_vegetables_legumes_pct=fvl,
        non_nutritive_sweeteners=nns,
    )
    out.update(score=res["score"], grade=res["grade"], components=res["components"],
               negative_points=res["negative_points"], positive_points=res["positive_points"],
               status="computed")
    if red is not None:
        out["red_meat_percent"] = round(red, 1)
    return out
