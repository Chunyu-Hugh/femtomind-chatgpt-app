"""Bounded archive tests; requires the existing scientific NumPy/SciPy environment."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy import stats

if 'jam24_reference' in sys.modules:
    archive = sys.modules['jam24_reference']
else:
    path = Path(__file__).resolve().parents[1] / 'reference_results/JAM24/build_archive.py'
    spec = importlib.util.spec_from_file_location('jam24_reference', path)
    archive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)


class ReferenceResultsTest(unittest.TestCase):
    def predictions(self):
        return {'thy': np.array([[0.0, 1.0], [2.0, 3.0]]),
                'res': np.array([[1.0, 1.0], [-1.0, -1.0]]),
                'rres': np.array([[1.0], [-1.0]]), 'nres': np.zeros((2, 1)),
                'shift': np.zeros((2, 2))}

    def test_mean_prediction_not_mean_chi2(self):
        data = {'value': np.array([1.0, 2.0]), 'alpha': np.ones(2)}
        result, mean, residual = archive.dataset_statistics(self.predictions(), data, lambda chi2, n: -1)
        self.assertEqual(result['chi2_data'], 0.0)
        self.assertEqual(result['mean_replica_data_chi2_diagnostic'], 2.0)
        self.assertEqual(result['chi2_cor'], 0.0)
        self.assertIsNone(result['normalization']['chi2'])
        self.assertIsNone(result['normalization']['value'])
        self.assertTrue(result['normalization']['missing_reason'])

    def test_normalization_links_and_unique_objective_penalty(self):
        source = {'conf': {'datasets': {'sia': {'norm': {1001: {'fixed': False}, 1002: {'fixed': 1001}}}}},
                  'order': [[2, 'sia', 1001]], 'params': np.array([[0.8], [1.0]])}
        values, root, status = archive.normalization_values(source, 'sia', 1002)
        self.assertEqual((root, status), (1001, 'linked'))
        result, _, _ = archive.dataset_statistics(self.predictions(), {'value': np.array([1., 2.]), 'alpha': np.ones(2)},
            lambda chi2, n: 0., values, 0.1, status)
        self.assertAlmostEqual(result['normalization']['chi2'], 1.)
        self.assertAlmostEqual(result['normalization']['uncertainty'], 0.1)
        self.assertEqual(result['chi2_objective_components']['normalization'], 0.)
        source['conf']['datasets']['sia']['norm'][1001]['fixed'] = 1002
        with self.assertRaisesRegex(ValueError, 'cycle'):
            archive.normalization_values(source, 'sia', 1002)

    def test_zscore_only_original_function_executes(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)
            source = 'def get_zscore(chi2_, dof):\n    p = stats.chi2.cdf(chi2_, dof)\n    return stats.norm.ppf(p)\n\nraise RuntimeError("Do not execute notebook")'
            (path / 'chi2_zscore_plot_paper.ipynb').write_text(json.dumps({'cells': [{'cell_type': 'code', 'source': [source]}]}))
            function, evidence = archive.original_zscore(path)
            self.assertLess(function(1., 5), 0.)
            self.assertGreater(function(15., 5), 0.)
            self.assertEqual(function(7.112478121631268, 11), stats.norm.ppf(stats.chi2.cdf(7.112478121631268, 11)))
            self.assertEqual(evidence['cell_index_zero_based'], 0)

    def test_npz_exact_roundtrip_and_object_rejection(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'numeric.npz'
            archive.save_numeric(path, {'float': np.array([1., np.nan]), 'empty': np.empty((2, 0)), 'text': np.array(['pion'], dtype=object)})
            with np.load(path, allow_pickle=False) as result:
                self.assertEqual(result['empty'].shape, (2, 0))
                self.assertEqual(result['text'][0], 'pion')
            archive.save_numeric(path, {'tag': np.array([None, 'uds', None], dtype=object)})
            self.assertEqual(archive.load_numeric(path)['tag'].tolist(), [None, 'uds', None])
            with self.assertRaises(ValueError):
                archive.save_numeric(path, {'nested': np.array([{}], dtype=object)})

    def test_no_correlations_is_not_a_display_zero(self):
        predictions = self.predictions()
        predictions['rres'] = np.empty((2, 0))
        row, _, _ = archive.dataset_statistics(predictions, {'value': np.array([1., 2.]), 'alpha': np.ones(2)}, lambda chi2, n: 0.)
        self.assertEqual(row['n_cor'], 0)
        self.assertIsNone(row['chi2_cor'])
        self.assertEqual(row['chi2_objective_components']['correlated'], 0.)

    def test_bad_alignment_and_nonfinite_predictions_fail(self):
        predictions = self.predictions()
        data = {'value': np.ones(3), 'alpha': np.ones(3)}
        with self.assertRaises(ValueError):
            archive.dataset_statistics(predictions, data, lambda chi2, n: 0.)
        predictions['thy'][0, 0] = np.nan
        with self.assertRaises(ValueError):
            archive.dataset_statistics(predictions, {'value': np.ones(2), 'alpha': np.ones(2)}, lambda chi2, n: 0.)

    def test_appendix_references_do_not_change_calculations(self):
        row = {'chi2_red': .646588920148297, 'z_score': -.81, 'chi2_cor': 6.88183278788, 'n_cor': 86,
               'normalization': {'chi2': .08469449, 'value': .994761582923497, 'uncertainty': .05486690876683897}}
        paper = {'publication_row': 'Table 10 row 1', 'source_url': 'catalog',
                 'chi2red_z_as_printed': '0.65 (-0.81)', 'chi2_cor_as_printed': '6.88',
                 'chi2_norm_as_printed': '0.08', 'n_cor_as_printed': '86', 'fitted_norm_as_printed': '0.995(55)'}
        before = json.dumps(row)
        comparison = archive.appendix_comparison(row, paper)
        self.assertTrue(all(c['status'] == 'match' for c in comparison['checks'].values()))
        self.assertEqual(json.dumps(row), before)
        paper['chi2_cor_as_printed'] = '9.99'
        self.assertEqual(archive.appendix_comparison(row, paper)['checks']['chi2_cor']['status'], 'disagreement')


if __name__ == '__main__':
    unittest.main()
