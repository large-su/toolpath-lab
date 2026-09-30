"""Parameter declaration, validation and defaults."""

from __future__ import annotations

import unittest

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind,
    ParameterSet,
    spec,
)


class ParameterSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.number = spec(
            "stepover_mm", "切宽", ParameterKind.FLOAT, 4.0,
            minimum=0.1, maximum=50.0, unit="mm",
        )

    def test_coerce_accepts_numbers_and_strings(self) -> None:
        self.assertEqual(self.number.coerce(6), 6.0)
        self.assertEqual(self.number.coerce("6.5"), 6.5)

    def test_coerce_rejects_out_of_range(self) -> None:
        with self.assertRaises(ParameterError) as context:
            self.number.coerce(0.0)
        self.assertIn("stepover_mm", str(context.exception))
        with self.assertRaises(ParameterError):
            self.number.coerce(1000)

    def test_coerce_rejects_garbage(self) -> None:
        with self.assertRaises(ParameterError):
            self.number.coerce("wide")
        with self.assertRaises(ParameterError):
            self.number.coerce(float("nan"))
        with self.assertRaises(ParameterError):
            self.number.coerce(True)

    def test_int_kind_requires_whole_numbers(self) -> None:
        item = spec("flutes", "刃数", ParameterKind.INT, 2, minimum=1, maximum=8)
        self.assertEqual(item.coerce("4"), 4)
        with self.assertRaises(ParameterError):
            item.coerce(2.5)

    def test_bool_kind(self) -> None:
        item = spec("flag", "开关", ParameterKind.BOOL, True)
        self.assertTrue(item.coerce("true"))
        self.assertFalse(item.coerce(0))
        with self.assertRaises(ParameterError):
            item.coerce("maybe")

    def test_choice_kind(self) -> None:
        item = spec(
            "mode", "模式", ParameterKind.CHOICE, "zigzag",
            choices=(Choice("zigzag", "往复"), Choice("one_way", "单向")),
        )
        self.assertEqual(item.coerce("one_way"), "one_way")
        with self.assertRaises(ParameterError) as context:
            item.coerce("spiral")
        self.assertIn("one_way", str(context.exception))

    def test_to_dict_is_json_ready(self) -> None:
        payload = self.number.to_dict()
        self.assertEqual(payload["key"], "stepover_mm")
        self.assertEqual(payload["kind"], "float")
        self.assertEqual(payload["min"], 0.1)


class ParameterSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parameters = ParameterSet(
            (
                spec("a", "A", ParameterKind.FLOAT, 1.0, minimum=0.0, maximum=10.0),
                spec("b", "B", ParameterKind.BOOL, False),
            )
        )

    def test_defaults(self) -> None:
        self.assertEqual(self.parameters.defaults(), {"a": 1.0, "b": False})

    def test_coerce_fills_defaults_and_ignores_unknown(self) -> None:
        values = self.parameters.coerce({"a": 5, "mystery": 7})
        self.assertEqual(values, {"a": 5.0, "b": False})

    def test_coerce_none_returns_defaults(self) -> None:
        self.assertEqual(self.parameters.coerce(None), self.parameters.defaults())

    def test_null_value_falls_back_to_default(self) -> None:
        self.assertEqual(self.parameters.coerce({"a": None})["a"], 1.0)

    def test_duplicate_keys_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ParameterSet(
                (
                    spec("a", "A", ParameterKind.FLOAT, 0.0),
                    spec("a", "A again", ParameterKind.FLOAT, 0.0),
                )
            )

    def test_addition_concatenates(self) -> None:
        extra = ParameterSet((spec("c", "C", ParameterKind.FLOAT, 2.0),))
        combined = self.parameters + extra
        self.assertEqual(len(combined), 3)
        self.assertEqual(combined.spec("c").default, 2.0)


if __name__ == "__main__":
    unittest.main()
