import unittest
from board_publish import identity, window

class BoardTest(unittest.TestCase):
    def test_stable_id(self):
        a='https://kick.com/zingisukan2525/videos/abc'
        self.assertEqual(identity(a,100,150),identity(a+'?tracking=1',100.,150.))
        self.assertNotEqual(identity(a,100,150),identity(a.replace('abc','def'),100,150))
        self.assertNotEqual(identity(a,100,150),identity(a,110,150))
    def test_whole_interval_and_padding(self):
        self.assertEqual(window(100,270),(70,300))
        self.assertEqual(window(5,45),(0,75))
        for a,b in [(-1,20),(20,20),(float('nan'),50),(0,700)]:
            with self.assertRaises(ValueError): window(a,b)

if __name__=='__main__':unittest.main()
