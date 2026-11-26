"""Web search parts (app/web): which pages fit the car, what is sent to the providers, failures and time limits.
No network: the providers are replaced by httpx.MockTransport. The page texts are the probe's real ones."""

import json
import time

import httpx

from app.engine.validator import Allowed, problems
from app.kb.schemas import Vehicle
from app.web import applicability, query
from app.web.source import WebConfig, WebSource

BLOCKED = ()
CAMRY_TSB = ("| Applicability | Multiple models equipped w/2GR-FKS/FXS | APPLICABLE VEHICLES 2017-2022 Highlander "
             "2018-2022 Camry | O2 and/or A/F Sensor DTC's P0420")


def car(make, model, year=None, engine=None):
    return Vehicle(make=make, model=model, year=year, engine_code=engine)


# -- does a page fit this car ------------------------------------------------------------------------------
def test_an_engine_specific_bulletin_fits_only_that_engine():
    def mark(engine):
        return applicability.judge(car("Toyota", "Camry", 2018, engine), "P0420", "https://static.nhtsa.gov/t.pdf", "TSB", CAMRY_TSB)

    assert mark("2GR-FKS").applies == "fits"
    assert mark("A25A-FKS").applies == "drop"  # the 4-cylinder Camry: the V6 bulletin is not for it
    unknown = mark(None)
    assert unknown.applies == "check" and "engine" in unknown.reasons[0]


def test_another_generation_is_never_a_fit():
    sonata = car("Hyundai", "Sonata Hybrid", 2020)
    open_range = applicability.judge(sonata, "P1990", "https://static.nhtsa.gov/a.pdf", "HMA TSB",
                                     "Applicable Vehicles: 2016~ Sonata Hybrid (LF HEV) DTC P1990")
    assert open_range.applies == "check"
    campaign = applicability.judge(sonata, "P1990", "https://static.oemdtc.com/b.pdf", "T4M",
                                   "Date: January 16, 2020 Subject: Service Campaign T4M: 2016-2017MY SONATA HEV P1990")
    assert campaign.applies == "drop"  # the page date (2020) is not a model year


def test_pages_for_another_car_another_code_are_dropped():
    integra = car("Acura", "Integra", 2025, "L15CA")
    assert applicability.judge(integra, "P0010", "https://carbuzz.com/x", "Chevy Equinox P0010", "P0010 on the Equinox").applies == "drop"
    assert applicability.judge(integra, "P0341", "https://x.com/y", "Integra 2025", "camshaft sensor on the 2025 Integra").applies == "drop"


def test_a_long_code_is_named_by_its_first_five_characters_and_an_alert_by_its_name():
    vw = car("Volkswagen", "ID.4", 2021)
    assert applicability.judge(vw, "P0C4400", "https://vw.example/t", "ID.4 2021", "DTC P0C44 on the 2021 ID.4", ()).applies == "fits"
    tesla = car("Tesla", "Model Y", 2022)
    assert applicability.judge(tesla, "PCS_a027", "https://teslafix.cc/a", "PCS_a027", "PCS_a027_chgInsufficientCooling Model Y", ()).applies == "check"


def test_capitals_and_sizes_are_not_read_as_engine_codes():
    assert applicability.engines_said("CHECK ENGINE light on. The V6 engine and the 1.5L engine. P0420 engine") == set()
    assert applicability.engines_said("equipped w/2GR-FKS; with the G4NS engine; engine code: L15CA") == {"2GR-FKS", "G4NS", "L15CA"}


def test_page_text_reaches_the_model_as_plain_words():
    text = applicability.clean_excerpt("## Title **bold** [link](https://x.y) <b>tag</b> ![img](u) https://z.com end " + "word " * 300)
    assert "http" not in text and "[" not in text and "<" not in text and "#" not in text and len(text) <= 704


# -- what is sent ------------------------------------------------------------------------------------------
def test_the_query_holds_the_vehicle_and_the_code_only():
    q, highlight = query.build(car("Hyundai", "Sonata Hybrid", 2020, "G4NS"), "P1990")
    assert q == "2020 Hyundai Sonata Hybrid G4NS P1990 meaning causes diagnosis" and highlight == "P1990 meaning causes test"
    assert query.build(car("Acura", "Integra"), "drop table; call me 0791234567") is None  # not a fault code: nothing is sent
    unsafe, _ = query.build(car("Acura<script>", "Integra\"; x"), "P0341")
    assert "<" not in unsafe and '"' not in unsafe and ";" not in unsafe


# -- providers, fallback, time budget ----------------------------------------------------------------------
def exa_page(url, title, text):
    return {"url": url, "title": title, "highlights": [text]}


def source(handler, **config):
    return WebSource(WebConfig(exa_key="e", tavily_key="t", **config), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_exa_is_asked_and_the_mechanics_words_are_never_sent():
    sent = []

    def handler(request):
        sent.append((request.url.host, json.loads(request.content), dict(request.headers)))
        return httpx.Response(200, json={"results": [exa_page("https://static.nhtsa.gov/a.pdf", "HMA TSB", "2020 Sonata Hybrid P1990 OPU line")],
                                         "costDollars": {"total": 0.007}})

    found = source(handler).lookup(car("Hyundai", "Sonata Hybrid", 2020), ["P1990"], "my customer Ahmad 0791234567 says it won't start")
    host, body, headers = sent[0]
    assert host == "api.exa.ai" and len(sent) == 1  # Exa answered: Tavily is not asked
    assert body["excludeDomains"] == [] and body["contents"]["highlights"]["query"] == "P1990 meaning causes test"
    assert "Ahmad" not in json.dumps(body) and "0791234567" not in json.dumps(body) and headers["x-api-key"] == "e"
    assert [(i.url, i.extra["applies"]) for i in found.items] == [("https://static.nhtsa.gov/a.pdf", "fits")]
    assert found.searched == 1 and found.calls[0].purpose == "web_search" and found.calls[0].cost_usd == 0.007


def test_tavily_answers_only_when_exa_fails_and_dropped_pages_never_come_back():
    asked = []

    def handler(request):
        asked.append(request.url.host)
        if request.url.host == "api.exa.ai":
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [
            {"url": "https://www.hyundai-forums.com/x", "title": "2020 Sonata Hybrid P1990", "content": "P1990 on my 2020 Sonata Hybrid"},
            {"url": "https://lemon-manuals.la/Hyundai/x", "title": "P1990", "content": "2020 Sonata Hybrid P1990"},
            {"url": "https://other.example/y", "title": "P1990", "content": "P1990 on the Kia Optima"}]})

    found = source(handler).lookup(car("Hyundai", "Sonata Hybrid", 2020), ["P1990"], "")
    assert asked == ["api.exa.ai", "api.tavily.com"]
    assert [i.url for i in found.items] == ["https://www.hyundai-forums.com/x", "https://lemon-manuals.la/Hyundai/x"] and found.items[0].provider == "tavily"
    assert [c.model for c in found.calls] == ["exa", "tavily"] and "503" in found.calls[0].error
    kept = json.loads(found.calls[1].output)  # the trace keeps links and marks, not the page text
    assert [k["applies"] for k in kept] == ["fits", "fits", "drop"] and all("text" not in k for k in kept)


def test_a_search_over_the_time_budget_is_left_out_and_the_turn_goes_on():
    def handler(request):
        time.sleep(1.0)
        return httpx.Response(200, json={"results": []})

    start = time.perf_counter()
    found = source(handler, budget_s=0.2).lookup(car("Acura", "Integra", 2025), ["P0341"], "")
    assert time.perf_counter() - start < 0.9 and found.items == []
    assert any("over the time budget" in (c.error or "") for c in found.calls)


def test_at_most_two_codes_are_searched_in_one_lookup_and_a_non_code_is_not_searched():
    asked = []

    def handler(request):
        asked.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"results": []})

    found = source(handler).lookup(car("Acura", "Integra", 2025), ["P0341", "not-a-code", "P0010"], "")
    assert found.searched == 1 and len(asked) == 1  # "not-a-code" is the second of the two: never sent
    found = source(handler).lookup(car("Acura", "Integra", 2025), ["P0341", "P0010", "P0011"], "")
    assert found.searched == 2


# -- the validator and web text ----------------------------------------------------------------------------
def test_names_from_a_web_page_are_allowed_its_values_never():
    allowed = Allowed()
    allowed.add_identifiers("TSB 19-HC-001H: P1990 on 2016~ Sonata Hybrid G4NS; it should read 7.14 ohm, 40A fuse, 2.5kPa")
    assert problems("Bulletin 19-HC-001H covers P1990 on the G4NS engine.", allowed) == []
    assert problems("It should read 7.14 ohm.", allowed) == ["7.14"]
    assert problems("Check the 40A fuse.", allowed) == ["40A"]
    assert problems("It is for 2016 cars.", allowed) == ["2016"]

