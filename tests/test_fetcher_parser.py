import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetcher import parse_csv


class FetcherParserTests(unittest.TestCase):
    def test_parse_csv_accepts_uk_style_headers(self):
        raw = """Holdings as of 01/Jan/2024

Ticker,Name,Sector,Asset Class,Weight (%),Market Value,Shares,Price,Market Currency,Exchange,Location
AAA,Alpha Corp,Technology,Equity,10.5,1000.0,100.0,10.0,USD,NYSE,US
BBB,Beta Corp,Health Care,Equity,8.25,900.0,90.0,10.0,USD,NYSE,US
"""

        holdings_date, rows = parse_csv(raw)

        self.assertEqual(holdings_date, __import__("datetime").date(2024, 1, 1))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ticker"], "AAA")
        self.assertEqual(rows[0]["weight_pct"], 10.5)

    def test_parse_csv_accepts_us_style_headers(self):
        raw = """Holdings as of 2024-01-01

Symbol,Company Name,Industry,Asset Type,Weight %,Market Value,Quantity,Price,Currency,Exchange,Location
CCC,Gamma Inc,Financials,Equity,4.5,500.0,50.0,10.0,USD,NYSE,US
DDD,Delta Inc,Utilities,Equity,3.0,300.0,30.0,10.0,USD,NYSE,US
"""

        holdings_date, rows = parse_csv(raw)

        self.assertEqual(holdings_date, __import__("datetime").date(2024, 1, 1))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ticker"], "CCC")
        self.assertEqual(rows[0]["weight_pct"], 4.5)


if __name__ == "__main__":
    unittest.main()
