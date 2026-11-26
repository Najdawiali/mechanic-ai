"""The marks the web probe puts on each search result (scripts/web_probe.py). No network."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx

_spec = importlib.util.spec_from_file_location("web_probe", Path(__file__).resolve().parents[1] / "scripts" / "web_probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["web_probe"] = probe  # dataclasses look the module up while the file loads
_spec.loader.exec_module(probe)

SONATA = probe.Case("sonata", "Hyundai", "Sonata Hybrid", 2020, "P1990", ("sonata",))


def test_year_marks():
    assert probe.year_fit("Applies to 2023–25 Integra ALL", 2025) == "match"
    assert probe.year_fit("Certain 2016-2019 Sonata Hybrid (LF HEV)", 2020) == "other years only"
    assert probe.year_fit("Applicable Vehicles: 2016~ Sonata Hybrid (LF HEV)", 2020) == "open range"
    assert probe.year_fit("2016 to 2019 Sonata", 2018) == "match"
    assert probe.year_fit("2020MY Sonata Hybrid", 2020) == "not stated"  # '2020MY' is not a clean year: a person reads it
    assert probe.year_fit("2020 Sonata Hybrid", 2020) == "match"
    assert probe.year_fit("P1990 means the transaxle", 2020) == "not stated"
    assert probe.year_fit("2018 Camry", None) == "not stated"
    # a publication date is not a model year (a real result: a campaign for 2016-2017 cars, sent in 2020)
    t4m = "Date: January 16, 2020 Subject: Service Campaign T4M: 2016-2017MY SONATA HEV MAIN RELAY"
    assert probe.year_fit(t4m, 2020) == "other years only"
    assert probe.year_fit("Posted: 2020-10-25. Fix for the 2019 Camry", 2020) == "other years only"
    assert probe.year_fit("©2020 Volkswagen Group of America. The ID.4", 2020) == "not stated"


def test_a_wrong_generation_bulletin_is_not_an_exact_result():
    r = probe.judge(SONATA, "https://static.nhtsa.gov/odi/tsbs/2019/MC-10160095-9999.pdf",
                    "TSB 19-HC-001H", "DTC P1990, P1B76 ... Applicable Vehicles: 2016~ Sonata Hybrid (LF HEV)")
    assert r.official and r.names_code and r.names_model and r.year == "open range" and r.domain == "static.nhtsa.gov"



def test_one_failing_provider_does_not_stop_the_others():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json={"results": [{"url": "https://www.example.com/p1990", "title": "P1990 2020 Sonata Hybrid",
                                                      "content": "P1990 on the 2020 Sonata Hybrid"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe.probe_case(client, {"tavily": "t", "exa": "e"}, SONATA)

    runs = {r.provider: r for r in asyncio.run(run())}
    assert runs["tavily"].ok and runs["tavily-official"].ok and not runs["exa"].ok
    hit = runs["tavily"].results[0]
    assert hit.names_code and hit.names_model and hit.year == "match" and not hit.official
