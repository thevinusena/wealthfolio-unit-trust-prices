"""Offline regression tests for the CeylonFunds Next.js extractor and CSV logic."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_prices as collector  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "nextjs_flight_sample.html"


class ExtractorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = FIXTURE.read_text(encoding="utf-8")

    def test_extracts_actual_publication_dates_and_buying_price(self) -> None:
        records, sources = collector.extract_price_records(
            self.page, "First Capital Money Market Fund", "FCMMF"
        )
        self.assertEqual([r.date for r in records], ["2026-11-19", "2026-11-20"])
        self.assertEqual([r.close for r in records], ["3836.76", "3837.66"])
        self.assertEqual(sources, {"utasl"})
        # The sample selling_price intentionally differs; the extractor must use buying_price.
        self.assertNotEqual(records[0].close, "3845.0000")

    def test_accepts_page_title_without_generic_fund_suffix(self) -> None:
        page = self.page.replace(
            "First Capital Money Market Fund", "Ceybank Century Growth"
        )
        records, _ = collector.extract_price_records(
            page, "Ceybank Century Growth Fund", "CCGF"
        )
        self.assertEqual(len(records), 2)

    def test_rejects_wrong_fund(self) -> None:
        with self.assertRaises(collector.FundUpdateError):
            collector.extract_price_records(self.page, "Another Fund", "FCMMF")

    def test_rejects_empty_points(self) -> None:
        payload = "0:" + json.dumps(
            {
                "fundName": "First Capital Money Market Fund",
                "currency": "LKR",
                "points": [],
            },
            separators=(",", ":"),
        )
        page = (
            "<html><body><script>self.__next_f.push([1,"
            + json.dumps(payload)
            + "]);</script></body></html>"
        )
        with self.assertRaises(collector.FundUpdateError):
            collector.extract_price_records(page, "First Capital Money Market Fund", "FCMMF")

    def test_rejects_conflicting_prices_for_same_date(self) -> None:
        payload = "0:" + json.dumps(
            {
                "fundName": "First Capital Money Market Fund",
                "currency": "LKR",
                "points": [
                    {"price_date": "2026-11-20T00:00:00.000Z", "buying_price": 3837.66, "source": "utasl"},
                    {"price_date": "2026-11-20T00:00:00.000Z", "buying_price": 3838.00, "source": "utasl"},
                ],
            },
            separators=(",", ":"),
        )
        page = (
            "<html><body><script>self.__next_f.push([1,"
            + json.dumps(payload)
            + "]);</script></body></html>"
        )
        with self.assertRaises(collector.FundUpdateError):
            collector.extract_price_records(page, "First Capital Money Market Fund", "FCMMF")

    def test_rejects_zero_bid_price(self) -> None:
        payload = "0:" + json.dumps(
            {
                "fundName": "First Capital Money Market Fund",
                "currency": "LKR",
                "points": [
                    {"price_date": "2026-11-20T00:00:00.000Z", "buying_price": 0, "source": "utasl"}
                ],
            },
            separators=(",", ":"),
        )
        page = (
            "<html><body><script>self.__next_f.push([1,"
            + json.dumps(payload)
            + "]);</script></body></html>"
        )
        with self.assertRaises(collector.FundUpdateError):
            collector.extract_price_records(page, "First Capital Money Market Fund", "FCMMF")


class MergeAndCsvTests(unittest.TestCase):
    def test_merge_preserves_old_dates_and_updates_existing_date(self) -> None:
        existing = {
            "2026-11-18": collector.PriceRecord("FCMMF", "2026-11-18", "3835.0000"),
            "2026-11-19": collector.PriceRecord("FCMMF", "2026-11-19", "3836.0000"),
        }
        incoming = [
            collector.PriceRecord("FCMMF", "2026-11-19", "3836.7600"),
            collector.PriceRecord("FCMMF", "2026-11-20", "3837.6600"),
        ]
        merged = collector.merge_history("FCMMF", incoming, existing)
        self.assertEqual([r.date for r in merged], ["2026-11-18", "2026-11-19", "2026-11-20"])
        self.assertEqual(merged[1].close, "3836.7600")

    def test_refuses_shortened_incoming_history(self) -> None:
        existing = {
            "2026-11-18": collector.PriceRecord("FCMMF", "2026-11-18", "3835.0000"),
            "2026-11-19": collector.PriceRecord("FCMMF", "2026-11-19", "3836.0000"),
        }
        incoming = [collector.PriceRecord("FCMMF", "2026-11-20", "3837.6600")]
        with self.assertRaises(collector.FundUpdateError):
            collector.merge_history("FCMMF", incoming, existing)

    def test_csv_header_and_order(self) -> None:
        records = [
            collector.PriceRecord("FCMMF", "2026-11-20", "3837.6600"),
            collector.PriceRecord("FCMMF", "2026-11-19", "3836.7600"),
        ]
        text = collector.csv_text(sorted(records, key=lambda item: item.date))
        lines = text.splitlines()
        self.assertEqual(lines[0], "symbol,date,close,currency")
        self.assertTrue(lines[1].startswith("FCMMF,2026-11-19,"))
        self.assertTrue(lines[2].startswith("FCMMF,2026-11-20,"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
