"""Natural-language question -> QuerySpec -> Postgres search -> grounded answer."""

import json
import os

from .concepts import GROUPS
from .pg import known_tags, search
from .query import QUERY_SPEC_SCHEMA, QuerySpec

CHAT_MODEL = os.getenv("OFF_CHAT_MODEL", "gpt-4o")

PARSE_PROMPT = """You turn shopping questions about packaged food into a search_products call.

Rules:
- Every hard requirement goes into a filter field. Only flavour/style/product-type wording
  goes into semantic_query (always fill it).
- Use Open Food Facts taxonomy ids, English, lowercase, hyphenated, "en:" prefix:
  categories like en:protein-bars, en:potato-crisps, en:dark-chocolates, en:breakfast-cereals,
  en:yogurts (granola and muesli -> en:breakfast-cereals; "snacks" -> en:snacks);
  ingredients like en:avocado-oil,
  en:olive-oil, en:cocoa, en:almond, en:oat.
- Ids are singular and specific. "Nuts" as a first ingredient -> first_ingredient_any with
  en:almond, en:roasted-almonds, en:peanut, en:roasted-peanuts, en:cashew-nuts, en:pecan-nut,
  en:walnut, en:pistachio-nuts, en:macadamia-nut, en:hazelnut, en:brazil-nut, en:nut.
- Label claims -> labels_all: gluten-free -> en:no-gluten, organic -> en:organic,
  vegan -> en:vegan, non-GMO -> en:no-gmos, kosher -> en:kosher.
- "per bar / per bag / per serving / in a bar" -> basis "serving". "per 100 g" or no unit
  context for densities -> basis "100g". If unclear for single-serve snacks use "serving".
- "without X" for a whole family -> exclude_groups. Available groups: {groups}.
  A single ingredient -> exclude_ingredients.
- "minimal ingredients" -> max_ingredients 5; "very minimal / very few" -> 4; and
  sort_by "ingredients_n" unless another sort is asked for.
- "high protein" without a number -> protein_g serving >= 15 (bars, snacks) and
  sort_by "protein_g_serving".
- Ingredient quantities ("70% cocoa", "at least 5 g almonds") -> ingredient_amounts.
  Set declared_only only if the user says the amount must be stated on the label.
- Do not invent constraints the user did not ask for.
"""

ANSWER_PROMPT = """You recommend packaged food products using ONLY the products provided.
Every product listed satisfies the database filters in "filters". Those filters are the
only requirements that were checked. If the question asks for something that is not in
"filters", or that "notes" says was ignored or relaxed, do not claim the products meet
it: say plainly that it was not verified. The products are already ranked: list every one
of them, numbered, in the order given; do not skip, reorder or add any. For each give, in
this order: name and brand, the numbers that answer the question (say per serving or per
100 g, and the serving size), and the ingredient facts that matter. If an ingredient
percentage is an estimate rather than declared on the label, say "estimated".
If there are no products, say so and suggest how to loosen the request.
Keep it concise."""

# Same question -> same answer: the answer sees a fixed, ranked slice of the results,
# and both calls run at temperature 0 with a fixed seed (OpenAI's best-effort determinism).
ANSWER_TOP_N = 5
SEED = 7


def _client():
    from openai import OpenAI

    return OpenAI()


def parse_question(question, client=None, model=CHAT_MODEL):
    client = client or _client()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        seed=SEED,
        messages=[
            {"role": "system", "content": PARSE_PROMPT.format(groups=", ".join(GROUPS))},
            {"role": "user", "content": question},
        ],
        tools=[{"type": "function", "function": QUERY_SPEC_SCHEMA}],
        tool_choice={"type": "function", "function": {"name": QUERY_SPEC_SCHEMA["name"]}},
    )
    args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
    return QuerySpec.from_dict(args)


def ground_spec(conn, spec):
    """Drop taxonomy ids that don't occur in the data; return notes on what changed."""
    notes = []
    for field, kind in (("categories_any", "category"), ("include_ingredients_all", "ingredient"),
                        ("first_ingredient_any", "ingredient"), ("labels_all", "label")):
        wanted = getattr(spec, field)
        kept = known_tags(conn, kind, wanted)
        dropped = [t for t in wanted if t not in kept]
        if dropped:
            notes.append(f"not enforced: unknown {kind} ids {dropped} in {field}")
            setattr(spec, field, kept)
    return notes


def retrieve(conn, spec, embedder):
    """Search, relaxing the category filter once if nothing matches."""
    vector = embedder.embed([spec.semantic_query])[0] if spec.semantic_query else None
    results, notes = search(conn, spec, vector), []
    if not results and spec.categories_any:
        notes.append(f"no match in categories {spec.categories_any}; searched all categories")
        spec.categories_any = []
        results = search(conn, spec, vector)
    return results, notes


def _facts(r):
    serv, per100 = r["nutrition"]["per_serving"], r["nutrition"]["per_100g"]
    keep = ("energy_kcal", "protein_g", "fat_g", "carbs_g", "sugars_g", "fiber_g", "sodium_mg")
    return {
        "name": r["name"], "brand": r["brand"], "code": r["code"], "url": r["url"],
        "serving_size": r["nutrition"]["serving_size"],
        "per_serving": {k: serv.get(k) for k in keep if serv.get(k) is not None},
        "per_100g": {k: per100.get(k) for k in keep if per100.get(k) is not None},
        "ingredients": (r["ingredients"]["text"] or "")[:500],
        "ingredient_count": r["ingredients"]["count_total"],
        "ingredient_amounts": [
            {"id": i["id"], "percent": i["percent"] if i["percent"] is not None else i["percent_estimate"],
             "declared": i["percent"] is not None}
            for i in r["ingredients"]["items"] if i["depth"] == 0
        ],
        "groups": {g: v["status"] for g, v in r["derived"]["groups"].items() if v["status"] != "none"},
        "free_of": [g for g, v in r["derived"]["groups"].items() if v["status"] == "none"],
        "labels": r["labels"][:10],
    }


def applied_filters(spec):
    """The hard filters of a spec that actually ran, for the answer to cite."""
    skip = {"semantic_query", "sort_by", "limit"}
    return {k: v for k, v in spec.__dict__.items() if k not in skip and v not in ([], None)}


def answer(question, results, notes, client=None, model=CHAT_MODEL, filters=None):
    client = client or _client()
    payload = {"question": question, "filters": filters or {}, "notes": notes,
               "products": [_facts(r) for r in results[:ANSWER_TOP_N]]}
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        seed=SEED,
        messages=[
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    return resp.choices[0].message.content


def ask(conn, question, embedder, client=None):
    client = client or _client()
    spec = parse_question(question, client)
    notes = ground_spec(conn, spec)
    results, more = retrieve(conn, spec, embedder)
    notes += more
    return {
        "question": question,
        "spec": spec.__dict__,
        "notes": notes,
        "products": [_facts(r) for r in results],
        "answer": answer(question, results, notes, client, filters=applied_filters(spec)),
    }
