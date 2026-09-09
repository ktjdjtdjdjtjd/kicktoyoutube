import importlib.util
import pathlib
import tempfile
import json
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('review', pathlib.Path(__file__).with_name('board_review.py'))
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def test_listing_paginates(self):
        with patch.object(review, 'request', side_effect=[{'items':[1], 'cursor':'a/b'}, {'items':[2], 'cursor':None}]) as req:
            self.assertEqual(review.listing('/api/edits'), [1,2])
            self.assertEqual(req.call_args.args[0], '/api/edits?cursor=a%2Fb')

    def test_repeated_cursor_fails(self):
        with patch.object(review, 'request', return_value={'items':[], 'cursor':'a'}):
            with self.assertRaises(ValueError):review.listing('/api/feedback')

    def test_feedback_exact_version_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'versions.json';p.write_text(json.dumps({'c/old':{'spec':'old.json'}}))
            with patch.object(review,'MANIFEST',p), patch.object(review,'listing',return_value=[{'candidate_id':'c','version':'new'}]):
                self.assertIsNone(review.feedback()[0]['local'])

    def test_revoked_job_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'queue.json';p.write_text(json.dumps({'job':{'candidate_id':'c','active':False}}))
            with patch.object(review,'QUEUE',p):
                with self.assertRaises(ValueError):review.local_job('c')


if __name__=='__main__':unittest.main()
