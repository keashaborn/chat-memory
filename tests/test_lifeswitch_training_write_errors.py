from __future__ import annotations

import unittest

from seebx.adapters.lifeswitch_training_writes_postgres import TrainingWriterError
from seebx.capabilities.training.write_errors import training_writer_http_error


class TrainingWriterHttpErrorTests(unittest.TestCase):
    def assert_mapping(self, sqlstate, status, detail, *, headers=None):
        mapped = training_writer_http_error(
            TrainingWriterError(sqlstate=sqlstate, detail="database detail")
        )
        self.assertEqual(mapped.status_code, status)
        self.assertEqual(mapped.detail, detail)
        self.assertEqual(mapped.headers, headers)

    def test_stable_sqlstate_mapping(self) -> None:
        self.assert_mapping("22023", 400, "database detail")
        self.assert_mapping("23502", 400, "invalid training write request")
        self.assert_mapping("P0002", 404, "database detail")
        self.assert_mapping("23505", 409, "database detail")
        self.assert_mapping("28000", 403, "training write not authorized")
        self.assert_mapping(
            "40001",
            503,
            "training write should be retried",
            headers={"Retry-After": "1"},
        )
        self.assert_mapping("42501", 503, "training write temporarily unavailable")
        self.assert_mapping(None, 500, "training write failed")

    def test_no_result_preserves_writer_specific_detail(self) -> None:
        mapped = training_writer_http_error(
            TrainingWriterError(detail="conditioning writer returned no session", no_result=True)
        )
        self.assertEqual(mapped.status_code, 500)
        self.assertEqual(mapped.detail, "conditioning writer returned no session")


if __name__ == "__main__":
    unittest.main()
