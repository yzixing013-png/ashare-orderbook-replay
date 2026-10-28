import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verify_market_slice import verify_small_cases, verify_synthetic_correction
from examples.generate_synthetic import generate
from market_slice import MarketReplay

class ReplayTests(unittest.TestCase):
    def test_synthetic_edge_cases(self):
        verify_small_cases()
    def test_snapshot_correction_and_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            generate(folder)
            verify_synthetic_correction(MarketReplay(folder,'2024-02-07','SH.TEST',1))
    def test_query_cutoff(self):
        with tempfile.TemporaryDirectory() as folder:
            generate(folder)
            replay=MarketReplay(folder,'2024-02-07','SH.TEST',1)
            self.assertEqual(replay.query('2024-02-07 09:30:00')['volume'],0)
            self.assertEqual(replay.query('2024-02-07 09:30:01')['volume'],50)

if __name__=='__main__': unittest.main()
