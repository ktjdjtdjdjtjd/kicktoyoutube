import random, unittest
import top10 as T

def chat(dur, rate, bursts=(), seed=1, laugh_in_burst=True):
    r = random.Random(seed); out = []
    for s in range(int(dur)):
        n = rate
        for (a, b, m) in bursts:
            if a <= s < b: n = m
        for _ in range(int(n) + (1 if r.random() < n - int(n) else 0)):
            hot = any(a <= s < b for a, b, _ in bursts)
            out.append({"rel": s + r.random(), "content": "wwww" if hot and laugh_in_burst and r.random() < .5 else "hi", "user": f"u{r.randrange(500)}"})
    return out

def flat_viewers(dur, v, steps=()):
    out = []
    for t in range(0, int(dur), 60):
        x = v
        for at, size in steps:
            if t >= at: x += size
        out.append({"t": t, "viewers": x})
    return out

class T10(unittest.TestCase):
    def test_ten_nonoverlapping_max5min(self):
        dur = 3 * 3600
        bursts = [(600 * i + 100, 600 * i + 160, 3) for i in range(1, 16)]
        r = T.analyze(chat(dur, .3, bursts), dur)
        segs = r["segments"]
        self.assertEqual(len(segs), 10)
        segs_s = sorted(segs, key=lambda s: s["start"])
        for a, b in zip(segs_s, segs_s[1:]):
            self.assertGreaterEqual(b["start"] - a["end"], T.MIN_GAP)
        for s in segs:
            self.assertLessEqual(s["length"], T.MAX_LEN)
            self.assertTrue(any(a <= s["peakAt"] < b for a, b, _ in bursts))

    def test_pure_raid_subtracted_and_not_picked(self):
        dur = 2 * 3600
        bursts = [(3000, 3060, 3)]
        v = flat_viewers(dur, 300, steps=[(1800, 3000)])  # raid at 30min, chat flat
        r = T.analyze(chat(dur, .3, bursts), dur, v)
        self.assertTrue(any(x["decision"] == "subtract" and abs(x["t"] - 1800) <= 120 for x in r["raids"]))
        top = r["segments"][0]
        self.assertTrue(3000 - 60 <= top["peakAt"] <= 3060 + 60, top)
        self.assertFalse(any(s["start"] <= 1800 <= s["end"] and s["rank"] <= 1 for s in r["segments"]))

    def test_raid_overlapping_real_hype_kept(self):
        dur = 2 * 3600
        bursts = [(1800, 1900, 4)]
        v = flat_viewers(dur, 300, steps=[(1800, 400)])
        r = T.analyze(chat(dur, .3, bursts), dur, v)
        raid = [x for x in r["raids"] if abs(x["t"] - 1800) <= 120]
        self.assertTrue(raid and raid[0]["decision"] == "keep_chat_hype", r["raids"])
        top = r["segments"][0]
        self.assertTrue(top["start"] <= 1850 <= top["end"])
        self.assertIn("採用", top["reason"])

    def test_raid_system_message(self):
        dur = 3600
        msgs = chat(dur, .3) + [{"rel": 1000, "content": "bigstreamer raided with 2000 viewers", "type": "raid"}]
        r = T.analyze(msgs, dur)
        self.assertEqual(r["raids"][0]["source"], "chat_system_message")

    def test_per_user_cap(self):
        msgs = [{"rel": 5, "content": "x", "user": "spam"} for _ in range(50)]
        c, _, _ = T.chat_bins(msgs, 60)
        self.assertEqual(c[0], T.PER_USER_CAP)

if __name__ == "__main__":
    unittest.main()
