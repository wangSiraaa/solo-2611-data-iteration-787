"""版本化业务分类测试：原子修订、历史可查、缺口识别。"""

import tempfile
import unittest
from pathlib import Path

from blend.classification import ClassificationStore
from blend.model import ValidationError


class ClassificationTest(unittest.TestCase):
    def test_revision_atomicity_and_history(self):
        cls = ClassificationStore(":memory:")
        self.assertIsNone(cls.revision)
        rev1 = cls.replace_classification(
            {"O1": "D1"}, {"L1": "FA", "L2": "FB"}, note="r1"
        )
        self.assertEqual(rev1, 1)
        self.assertEqual(cls.departments(), ("D1",))
        self.assertEqual(cls.families(), ("FA", "FB"))
        self.assertEqual(cls.department_of("O1"), "D1")
        self.assertIsNone(cls.department_of("NOPE"))
        # 第二次整体修订
        rev2 = cls.replace_classification(
            {"O1": "D2", "O2": "D2"}, {"L1": "FA"}, note="r2"
        )
        self.assertEqual(rev2, 2)
        # 历史修订永久可查，旧 rev 保持原样
        self.assertEqual(cls.order_departments(rev1), {"O1": "D1"})
        self.assertEqual(cls.lot_families(rev1), {"L1": "FA", "L2": "FB"})
        self.assertEqual(cls.order_departments(rev2), {"O1": "D2", "O2": "D2"})
        self.assertEqual(len(cls.revisions()), 2)

    def test_invalid_inputs_rollback_revision(self):
        cls = ClassificationStore(":memory:")
        cls.replace_classification({"O1": "D1"}, {"L1": "FA"})
        with self.assertRaises(ValidationError):
            cls.replace_classification({"": "D1"}, {"L1": "FA"})
        with self.assertRaises(ValidationError):
            cls.replace_classification({"O1": ""}, {"L1": "FA"})
        with self.assertRaises(ValidationError):
            cls.replace_classification({"O1": "D1"}, {"L1": ""})
        # 失败后修订号不动
        self.assertEqual(cls.revision, 1)

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cls.db"
            cls = ClassificationStore(path)
            cls.replace_classification({"O1": "D1"}, {"L1": "FA"}, note="持久")
            cls.close()
            reopened = ClassificationStore(path)
            self.assertEqual(reopened.revision, 1)
            self.assertEqual(reopened.order_departments(), {"O1": "D1"})
            self.assertEqual(reopened.lot_families(), {"L1": "FA"})
            self.assertEqual(reopened.families(), ("FA",))
            reopened.close()

    def test_unknown_rev_rejected(self):
        cls = ClassificationStore(":memory:")
        with self.assertRaises(ValidationError):
            cls.order_departments(99)
        cls.replace_classification({"O1": "D1"}, {"L1": "FA"})
        with self.assertRaises(ValidationError):
            cls.families(99)


if __name__ == "__main__":
    unittest.main()
