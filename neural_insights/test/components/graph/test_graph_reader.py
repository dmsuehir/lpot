# -*- coding: utf-8 -*-
# Copyright (c) 2023 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Test Graph Reader."""

import unittest
from unittest.mock import MagicMock, patch

from neural_insights.components.graph.graph import Graph
from neural_insights.components.graph.graph_reader import GraphReader


class TestGraphReader(unittest.TestCase):
    """Test GraphReader class."""

    @patch("neural_insights.components.graph.graph_reader.Collapser")
    @patch("neural_insights.components.graph.graph_reader.ModelRepository")
    def test_read(
        self,
        mocked_model_repository: MagicMock,
        mocked_collapser: MagicMock,
    ) -> None:
        """Test read."""
        model_path = "/path/to/model.file"
        expanded_groups = ["a", "b", "a/c"]

        model_graph = Graph()
        collapsed_graph = Graph()

        mocked_model = MagicMock("neural_insights.components.model.Model").return_value
        mocked_model.get_model_graph.return_value = model_graph

        mocked_model_repository.return_value.get_model.return_value = mocked_model

        mocked_collapser.return_value.collapse.return_value = collapsed_graph

        graph_reader = GraphReader()
        self.assertIs(collapsed_graph, graph_reader.read(model_path, expanded_groups))

        mocked_model_repository.assert_called_once()
        mocked_model_repository.return_value.get_model.assert_called_once_with(model_path)

        mocked_model.get_model_graph.assert_called_once()

        mocked_collapser.assert_called_once_with(expanded_groups)
        mocked_collapser.return_value.collapse.assert_called_once_with(model_graph)


if __name__ == "__main__":
    unittest.main()
