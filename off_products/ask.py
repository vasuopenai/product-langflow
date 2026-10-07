"""Natural-language question -> QuerySpec -> Postgres search -> grounded answer."""

import json
import os

from .concepts import GROUPS
from .pg import known_tags, search
from .retail import retailer_info
from .query import QUERY_SPEC_SCHEMA, QuerySpec

CHAT_MODEL = os.getenv("OFF_CHAT_MODEL", "gpt-4o")

PARSE_PROMPT = """You turn shopping questions about packaged food into a search_products call.

Rules:
- Every hard requirement goes into a filter field. Only flavour/style/product-type wording
  goes into semantic_query (always fill it).
- Use Open Food Facts taxonomy ids, English, lowercase, hyphenated, "en:" prefix:
  categories like en:protein-bars, en:potato-crisps, en:dark-chocolates, en:breakfast-cereals,
  en:yogurts; ingredients like en:avocado-oil, en:olive-oil, en:cocoa-mass, en:almond, en:oat.
- "per bar / per bag / per serving / in a bar" -> basis "serving". "per 100 g" or no unit
  context for densities -> basis "100g". If unclear for single-serve snacks use "serving".
- "without X" for a whole family -> exclude_groups. Available groups: {groups}.
  A single ingredient -> exclude_ingredients.
- "minimal ingredients" -> max_ingredients 5; "very minimal / very few" -> 4; and
  sort_by "ingredients_n" unless another sort is asked for.
- "high protein" without a number -> protein_g serving >= 15 (bars, snacks) and
  sort_by "protein_g_serving".
- Ingredient quantities ("70% cocoa", "at least 5 g almonds") -> ingredient_amounts.
- "at Kroger" / "from Kroger" / "Kroger sells" -> retailers_any ["kroger"].
- Do not invent constraints the user did not ask for.
"""

ANSWER_PROMPT = """You recommend packaged food products using ONLY the products provided.
Every product listed already satisfies the user's hard requirements (they were filtered
in a database). Recommend the best 3-5, and for each give: name and brand, the numbers
that answer the question (say per serving or per 100 g, and the serving size), and the
ingredient facts that matter, and where it is sold with price and aisle when
"retailers" is present (say the price date). If an ingredient percentage is an estimate rather than
declared on the label, say "estimated". Mention any relaxed filters from the notes.
If there are no products, say so and suggest how to loosen the request.
Keep it concise."""


def _client():
    from openai import OpenAI

    return OpenAI()


def parse_question(question, client=None, model=CHAT_MODEL):
    client = client or _client()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
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
                        ("first_ingredient_any", "ingredient"), ("labels_all", "label"),
                        ("retailers_any", "retailer")):
        wanted = getattr(spec, field)
        kept = known_tags(conn, kind, wanted)
        dropped = [t for t in wanted if t not in kept]
        if dropped:
            notes.append(f"ignored unknown {kind} ids {dropped}")
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
    stores = retailer_info(conn, [r["code"] for r in results], ph="%s")
    for r in results:
        r["retailers"] = stores.get(r["code"], [])
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
        "labels": r["labels"][:10],
        "retailers": r.get("retailers", []),
    }


def answer(question, results, notes, client=None, model=CHAT_MODEL):
    client = client or _client()
    payload = {"question": question, "notes": notes, "products": [_facts(r) for r in results]}
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
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
        "answer": answer(question, results, notes, client),
    }
