"""Гарантирует, что корень репозитория (и пакет mars/) виден тестам
независимо от того, как запущен pytest."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
