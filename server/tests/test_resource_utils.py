"""Tests for K8s resource quantity parsing."""

import pytest

from src.services.k8s.resource_utils import parse_k8s_quantity


class TestParseK8sQuantity:
    def test_plain_number(self):
        assert parse_k8s_quantity("100") == 100

    def test_gi(self):
        assert parse_k8s_quantity("5Gi") == 5 * (2**30)

    def test_mi(self):
        assert parse_k8s_quantity("512Mi") == 512 * (2**20)

    def test_ki(self):
        assert parse_k8s_quantity("1Ki") == 1024

    def test_decimal_gi(self):
        assert parse_k8s_quantity("1.5Gi") == int(1.5 * (2**30))

    def test_g(self):
        assert parse_k8s_quantity("1G") == 10**9

    def test_m(self):
        assert parse_k8s_quantity("500M") == 500 * 10**6

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_k8s_quantity("invalid")

    def test_comparison_larger(self):
        assert parse_k8s_quantity("10Gi") > parse_k8s_quantity("5Gi")

    def test_comparison_equal(self):
        assert parse_k8s_quantity("1Gi") == parse_k8s_quantity("1Gi")
