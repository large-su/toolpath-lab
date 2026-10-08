"""毛坯：模型的最小六面体包容体。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError, RegistryError
from toolpath_lab.core.mesh import Mesh, ModelLibrary
from toolpath_lab.core.stock import (
    STOCKS,
    ModelStock,
    NoStock,
    build_stock,
    stock_catalog,
)


def plate(side: float = 60.0, top: float = 10.0) -> Mesh:
    """一块平板：底面 Z=0、顶面 Z=top。"""

    half = side / 2.0
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    triangles = []
    for index in range(4):
        x0, y0 = corners[index]
        x1, y1 = corners[(index + 1) % 4]
        triangles.append([(x0, y0, top), (x1, y1, top), (0.0, 0.0, top)])
        triangles.append([(x0, y0, 0.0), (0.0, 0.0, 0.0), (x1, y1, 0.0)])
    return Mesh(np.array(triangles, dtype=np.float64))


class StockCatalogTests(unittest.TestCase):
    def test_registered_stocks(self) -> None:
        self.assertEqual(STOCKS.ids(), ["none", "model"])

    def test_catalog_publishes_labels_and_parameters(self) -> None:
        entries = {entry["id"]: entry for entry in stock_catalog()}
        self.assertEqual(entries["none"]["label"], "不使用毛坯")
        self.assertEqual(entries["none"]["parameters"], [])
        self.assertEqual(
            [item["key"] for item in entries["model"]["parameters"]],
            ["margin_xy_mm", "margin_top_mm"],
        )

    def test_unknown_stock_raises(self) -> None:
        with self.assertRaises(RegistryError):
            build_stock("casting", {})


class NoStockTests(unittest.TestCase):
    def test_no_stock_has_no_box(self) -> None:
        stock = build_stock("none", {})
        self.assertIsInstance(stock, NoStock)
        self.assertFalse(stock.is_set)
        self.assertIsNone(stock.box())
        described = stock.describe()
        self.assertFalse(described["is_set"])
        self.assertIsNone(described["bounds_mm"])

    def test_top_of_an_unset_stock_is_a_parameter_error(self) -> None:
        with self.assertRaises(ParameterError):
            build_stock("none", {}).top_mm


class ModelStockTests(unittest.TestCase):
    def _model(self, **kwargs):
        return ModelLibrary().add("plate.stl", b"", mesh=plate(**kwargs))

    def test_stock_needs_a_model(self) -> None:
        with self.assertRaises(ParameterError):
            build_stock("model", {})
        with self.assertRaises(ParameterError):
            ModelStock().box()

    def test_default_box_is_the_bounding_box(self) -> None:
        stock = build_stock("model", {}, model=self._model(side=60.0, top=10.0))
        self.assertEqual(stock.box(), (-30.0, 30.0, -30.0, 30.0, 0.0, 10.0))
        self.assertEqual(stock.top_mm, 10.0)
        self.assertEqual(stock.bottom_mm, 0.0)
        self.assertTrue(stock.is_set)

    def test_margins_grow_the_stock(self) -> None:
        stock = build_stock(
            "model",
            {"margin_xy_mm": 5.0, "margin_top_mm": 2.5},
            model=self._model(side=60.0, top=10.0),
        )
        self.assertEqual(stock.box(), (-35.0, 35.0, -35.0, 35.0, 0.0, 12.5))
        self.assertEqual(stock.top_mm, 12.5)

    def test_describe_publishes_bounds_and_size(self) -> None:
        stock = build_stock("model", {}, model=self._model(side=60.0, top=10.0))
        described = stock.describe()
        self.assertEqual(described["kind"], "model")
        self.assertTrue(described["is_set"])
        self.assertEqual(described["size_mm"], [60.0, 60.0, 10.0])
        self.assertEqual(described["top_mm"], 10.0)
        self.assertEqual(described["parameters"], {"margin_xy_mm": 0.0, "margin_top_mm": 0.0})
        self.assertIn("包容体", described["note"])

    def test_parameters_are_validated(self) -> None:
        with self.assertRaises(ParameterError):
            build_stock("model", {"margin_xy_mm": -1.0}, model=self._model())


if __name__ == "__main__":
    unittest.main()
