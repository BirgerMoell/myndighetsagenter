import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from myndighetskort import build_gazetteer, parse_letter  # noqa: E402
from myndighetskort.parser import dehyphenate  # noqa: E402

GAZ = build_gazetteer(["Testverket", "Tullverket", "Kronofogdemyndigheten", "Kammarkollegiet"])

STANDARD = """# Regleringsbrev för budgetåret 2026 avseende Testverket

2 bilagor

Riksdagen har beslutat (prop. 2025/26:1 utg.omr. 3, bet. 2025/26:SkU1, rskr. 2025/26:97).

## VERKSAMHET

Testverket ska bidra till ett väl fungerande samhälle.

### 1 Mål och återrapporteringskrav

***Skattefelet***

Skatte- verket ska redovisa verksamhets- och it-utveckling.

***Återrapportering***

Testverket ska redovisa måluppfyllelsen.

### 3 Uppdrag

***Pågående uppdrag***

**Kriminell ekonomi**

Testverket ska tillsammans med Tullverket och Kronofogdemyndigheten analysera:

- hur stora beloppen är i varje sektor,
- vilka åtgärder som har gett effekt.

Uppdraget ska redovisas till Regeringskansliet (Finansdepartementet) senast den 11 december 2026.

***Prognoser***

Testverket ska redovisa prognoser i Hermes den 18 januari och 3 maj.

## FINANSIERING

### 4 Anslag

#### 4.1 Tilldelade anslag/anslagsposter (belopp angivna i tkr)

***Utgiftsområde 3 Skatt, tull och exekution***

**1:1 Testverket (Ramanslag)**

| Nomenklatur | Namn | Belopp |
| --- | --- | --- |
| Disponeras av Testverket | | 1 200 |
| ap.1 | Förvaltning (ram) | 1 000 |
| ap.2 | Bidrag (ram) | 200 |

**Villkor för anslag 1:1**

***ap.1 Förvaltning***

Högst 30 000 kronor får användas för ombud.

#### 4.3 Finansiella villkor

| Anslag/ap | Anslagskredit | Indrag av anslagsbelopp |
| --- | --- | --- |
| 1:1 Testverket | | |
| ap.1 | 30 | 0 |

### 5 Övriga villkor

#### 5.1 Låneramar och krediter

| | |
| --- | --- |
| Räntekontokredit | 617 |

#### 5.2 Utbetalningsplan

| Utbetalningsdatum | Belopp |
| --- | --- |
| 2026-01-25 | 600 |
| 2026-02-25 | 600 |
"""

VARIANT = """# Regleringsbrev för budgetåret 2025 avseende Testverket

# VERKSAMHET

## 1 Mål och återrapporteringskrav

#### Mål

### Rekryteringsmål

Lärosätet ska redovisa åtgärder.

## 3 Uppdrag

#### 1. Utökad kapacitet

Uppdraget ska redovisas senast den 15 april 2025.

# FINANSIERING

## 4 Anslag

### 4.1 Tilldelade anslag/anslagsposter (belopp angivna i tkr)

#### Utgiftsområde 16 Utbildning

##### 2:13 Testverket: Utbildning (Ramanslag)

| Disponeras av Testverket | 500 |
| --- | --- |
| ap.1 | Takbelopp (ram) | 500 |

Villkor för anslag 2:13

###### ap.1 Takbelopp

Anslagsposten får användas för utbildning.
"""


def card(md, year="2026"):
    return parse_letter({"id": 1, "name": "Testverket", "year": year, "category": "Myndighet",
                         "type": "Regleringsbrev", "date": year + "-01-01", "md": md}, GAZ)


def test_dehyphenate():
    assert dehyphenate("Skatte- verket") == "Skatteverket"
    assert dehyphenate("verksamhets- och it-utveckling") == "verksamhets- och it-utveckling"


def test_standard_verksamhet():
    c = card(STANDARD)
    assert c["mission"].startswith("Testverket ska bidra")
    assert c["riksdag_refs"]["prop"] == ["2025/26:1"]
    assert c["n_attachments_stated"] == 2
    assert [g["title"] for g in c["goals"]] == ["Skattefelet"]
    assert "Återrapportering:" in c["goals"][0]["text"]
    assert "Skatteverket" in c["goals"][0]["text"]
    a, p = c["assignments"]
    assert a["title"] == "Kriminell ekonomi" and a["group"] == ["Pågående uppdrag"] and a["ongoing"]
    assert a["deadlines"] == ["2026-12-11"]
    assert a["recipients"] == ["Regeringskansliet (Finansdepartementet)"]
    assert a["partners"] == ["Kronofogdemyndigheten", "Tullverket"]
    assert len(a["sub_questions"]) == 2
    assert "analysera" in a["task_types"]
    assert p["forecast_dates"] == ["2026-01-18", "2026-05-03"] and "hermes" in p["channels"]


def test_standard_finance():
    c = card(STANDARD)
    (ap,) = c["appropriations"]
    assert ap["code"] == "1:1" and ap["kind"] == "Ramanslag"
    assert ap["expenditure_area"]["number"] == 3
    d = ap["disposals"][0]
    assert d["by"] == "Testverket" and d["amount_tkr"] == 1200 and d["sum_ok"] is True
    assert [i["amount_tkr"] for i in d["items"]] == [1000, 200]
    assert ap["caps"] == [{"ap": "1", "max": 30000, "unit": "kronor"}]
    assert c["financial_terms"][0]["Anslagskredit"] == 30
    assert c["loans"] == {"Räntekontokredit": 617}
    assert c["summary"]["payment_plan_total_tkr"] == 1200
    assert c["warnings"] == []


def test_variant_layout():
    c = card(VARIANT, "2025")
    assert c["layout"] == "2024-12"
    assert c["goals"][0]["title"] == "Rekryteringsmål" and c["goals"][0]["group"] == ["Mål"]
    assert c["assignments"][0]["title"] == "Utökad kapacitet"
    assert c["assignments"][0]["deadlines"] == ["2025-04-15"]
    ap = c["appropriations"][0]
    assert ap["code"] == "2:13" and ap["disposals"][0]["amount_tkr"] == 500
    assert ap["conditions"][0]["ap"] == "1"
