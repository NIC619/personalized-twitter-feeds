"""Tests for EmbeddingManager."""

import math

import pytest

from src.embeddings import EmbeddingManager


@pytest.fixture
def manager():
    return EmbeddingManager()


class TestCosineSimilarity:
    def test_identical_vectors(self, manager):
        vec = [1.0, 2.0, 3.0]
        assert manager.cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self, manager):
        vec1 = [1.0, 0.0]
        vec2 = [0.0, 1.0]
        assert manager.cosine_similarity(vec1, vec2) == pytest.approx(0.0)

    def test_opposite_vectors(self, manager):
        vec1 = [1.0, 0.0]
        vec2 = [-1.0, 0.0]
        assert manager.cosine_similarity(vec1, vec2) == pytest.approx(-1.0)

    def test_empty_vec1(self, manager):
        assert manager.cosine_similarity([], [1.0, 2.0]) == 0.0

    def test_empty_vec2(self, manager):
        assert manager.cosine_similarity([1.0, 2.0], []) == 0.0

    def test_both_empty(self, manager):
        assert manager.cosine_similarity([], []) == 0.0

    def test_zero_norm_vec1(self, manager):
        assert manager.cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_zero_norm_vec2(self, manager):
        assert manager.cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0

    def test_known_angle(self, manager):
        # 45-degree angle: cos(45°) ≈ 0.7071
        vec1 = [1.0, 0.0]
        vec2 = [1.0, 1.0]
        expected = 1.0 / math.sqrt(2)
        assert manager.cosine_similarity(vec1, vec2) == pytest.approx(expected)
