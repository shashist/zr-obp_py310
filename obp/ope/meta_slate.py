# Copyright (c) Yuta Saito, Yusuke Narita, and ZOZO Technologies, Inc. All rights reserved.
# Licensed under the Apache 2.0 License.

"""Off-Policy Evaluation Class to Streamline OPE."""
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import matplotlib.pyplot as plt
import numpy as np
from pandas import DataFrame
import seaborn as sns
from sklearn.utils import check_scalar

from ..types import BanditFeedback
from ..utils import check_confidence_interval_arguments
from .estimators_slate import BaseSlateOffPolicyEstimator
from .regression_model_slate import SlateRegressionModel


logger = getLogger(__name__)


@dataclass
class SlateOffPolicyEvaluation:
    """Class to conduct slate off-policy evaluation by multiple off-policy estimators simultaneously.

    Parameters
    -----------
    bandit_feedback: BanditFeedback
        Logged bandit feedback data used for off-policy evaluation for the slate recommendation setting.

    ope_estimators: List[BaseSlateOffPolicyEstimator]
        List of OPE estimators used to evaluate the policy value of evaluation policy.
        Estimators must follow the interface of `obp.ope.BaseSlateOffPolicyEstimator`.

    base_regression_model: Optional[SlateRegressionModel] = None
        Baseline regression model for :math:`\\hat{Q}_k` in Cascade-DR estimator.

    is_factorizable: bool, default=False
        If the behavior and evaluation policies are factorizable or not.

    Examples
    ----------

    .. code-block:: python

        # a case for implementing OPE of the uniform random policy
        # using log data generated by a linear behavior policy
        >>> from obp.ope import SlateOffPolicyEvaluation, SlateStandardIPS as SIPS
        >>> from obp.dataset import (
                logistic_reward_function,
                linear_behavior_policy_logit,
                SyntheticSlateBanditDataset,
            )

        # (1) Synthetic Data Generation
        >>> dataset = SyntheticSlateBanditDataset(
                n_unique_action=10,
                len_list=3,
                dim_context=2,
                reward_type="binary",
                reward_structure="cascade_additive",
                click_model=None,
                behavior_policy_function=behavior_policy_function,
                base_reward_function=base_reward_function,
            )
        >>> bandit_feedback = dataset.obtain_batch_bandit_feedback(
                n_rounds=1000,
                return_pscore_item_position=True
            )
        >>> bandit_feedback.keys()
        dict_keys([
            'n_rounds',
            'n_unique_action',
            'slate_id',
            'context',
            'action_context',
            'action',
            'position',
            'reward',
            'expected_reward_factual',
            'pscore_cascade',
            'pscore',
            'pscore_item_position'
        ])

        # (2) Evaluation Policy Definition (Off-Policy Learning)
        >>> random_dataset = dataset = SyntheticSlateBanditDataset(
                n_unique_action=10,
                len_list=3,
                dim_context=2,
                reward_type="binary",
                reward_structure="cascade_additive",
                click_model=None,
                behavior_policy_function=None,  # set to uniform random
                base_reward_function=base_reward_function,
            )
        >>> random_feedback = random_dataset.obtain_batch_bandit_feedback(
                n_rounds=n_rounds_test,
                return_pscore_item_position=True,
            )

        # (3) Off-Policy Evaluation
        >>> ope = SlateOffPolicyEvaluation(bandit_feedback=bandit_feedback, ope_estimators=[SIPS(len_list=3)])
        >>> estimated_policy_value = ope.estimate_policy_values(
                evaluation_policy_pscore=bandit_feedback["pscore"],
                evaluation_policy_pscore_item_position=bandit_feedback["pscore_item_position"],
                evaluation_policy_pscore_cascade=bandit_feedback["pscore_cascade"]
            )
        >>> estimated_policy_value
        {'sips': 1.894}

    """

    bandit_feedback: BanditFeedback
    ope_estimators: List[BaseSlateOffPolicyEstimator]
    base_regression_model: Optional[SlateRegressionModel] = None
    is_factorizable: bool = False

    def __post_init__(self) -> None:
        """Initialize class."""
        self.n_rounds = self.bandit_feedback["n_rounds"]
        self.len_list = int((self.bandit_feedback["slate_id"] == 0).sum())
        self.n_unique_action = self.bandit_feedback["n_unique_action"]

        for key_ in ["slate_id", "position", "reward"]:
            if key_ not in self.bandit_feedback:
                raise RuntimeError(f"Missing key of {key_} in 'bandit_feedback'.")
        self.ope_estimators_ = dict()
        for estimator in self.ope_estimators:
            self.ope_estimators_[estimator.estimator_name] = estimator

    def _create_estimator_inputs(
        self,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
    ) -> Dict[str, np.ndarray]:
        """Create input dictionary to estimate policy value by subclasses of `BaseSlateOffPolicyEstimator`"""
        if (
            evaluation_policy_pscore is None
            and evaluation_policy_pscore_item_position is None
            and evaluation_policy_pscore_cascade is None
        ):
            raise ValueError(
                "one of evaluation_policy_pscore, evaluation_policy_pscore_item_position, or evaluation_policy_pscore_cascade must be given"
            )

        estimator_inputs = {
            input_: self.bandit_feedback[input_]
            for input_ in [
                "slate_id",
                "action",
                "reward",
                "position",
                "pscore",
                "pscore_item_position",
                "pscore_cascade",
            ]
            if input_ in self.bandit_feedback
        }
        estimator_inputs["evaluation_policy_pscore"] = evaluation_policy_pscore
        estimator_inputs[
            "evaluation_policy_pscore_item_position"
        ] = evaluation_policy_pscore_item_position
        estimator_inputs[
            "evaluation_policy_pscore_cascade"
        ] = evaluation_policy_pscore_cascade
        estimator_inputs[
            "evaluation_policy_action_dist"
        ] = evaluation_policy_action_dist

        q_hat_for_counterfactual_actions = self.base_regression_model.fit_predict(
            context=self.bandit_feedback["context"],
            action=self.bandit_feedback["action"],
            reward=self.bandit_feedback["reward"],
            pscore_cascade=self.bandit_feedback["pscore_cascade"],
            evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
            evaluation_policy_action_dist=evaluation_policy_action_dist,
        )
        estimator_inputs[
            "q_hat_for_counterfactual_actions"
        ] = q_hat_for_counterfactual_actions

        return estimator_inputs

    def estimate_policy_values(
        self,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
    ) -> Dict[str, float]:
        """Estimate the policy value of evaluation policy.

        Parameters
        ------------
        evaluation_policy_pscore: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities of evaluation policy, i.e., :math:`\\pi_e(a_t|x_t)`.

        evaluation_policy_pscore_item_position: array-like, shape (<= n_rounds * len_list,)
            Marginal action choice probabilities of the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(a_{t, k}|x_t)`.

        evaluation_policy_pscore_cascade: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities above the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(\\{a_{t, j}\\}_{j \\le k}|x_t)`.

        evaluation_policy_action_dist: array-like, shape (n_rounds * len_list * n_unique_action, )
             Action choice probabilities of evaluation policy for all possible actions
             , i.e., :math:`\\pi_e({a'}_t(k) | x_t, a_t(1), \\ldots, a_t(k-1)) \\forall {a'}_t(k) \\in \\mathcal{A}`.

        Returns
        ----------
        policy_value_dict: Dict[str, float]
            Dictionary containing estimated policy values by OPE estimators.

        """
        policy_value_dict = dict()
        estimator_inputs = self._create_estimator_inputs(
            evaluation_policy_pscore=evaluation_policy_pscore,
            evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
            evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
            evaluation_policy_action_dist=evaluation_policy_action_dist,
        )
        for estimator_name, estimator in self.ope_estimators_.items():
            policy_value_dict[estimator_name] = estimator.estimate_policy_value(
                **estimator_inputs
            )

        return policy_value_dict

    def estimate_intervals(
        self,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
        alpha: float = 0.05,
        n_bootstrap_samples: int = 100,
        random_state: Optional[int] = None,
    ) -> Dict[str, Dict[str, float]]:
        """Estimate confidence intervals of policy values using nonparametric bootstrap procedure.

        Parameters
        ------------
        evaluation_policy_pscore: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities of evaluation policy, i.e., :math:`\\pi_e(a_t|x_t)`.

        evaluation_policy_pscore_item_position: array-like, shape (<= n_rounds * len_list,)
            Marginal action choice probabilities of the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(a_{t, k}|x_t)`.

        evaluation_policy_pscore_cascade: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities above the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(\\{a_{t, j}\\}_{j \\le k}|x_t)`.

        evaluation_policy_action_dist: array-like, shape (n_rounds * len_list * n_unique_action, )
            Action choice probabilities of evaluation policy for all possible actions
            , i.e., :math:`\\pi_e({a'}_t(k) | x_t, a_t(1), \\ldots, a_t(k-1)) \\forall {a'}_t(k) \\in \\mathcal{A}`.

        alpha: float, default=0.05
            Significance level.

        n_bootstrap_samples: int, default=100
            Number of resampling performed in the bootstrap procedure.

        random_state: int, default=None
            Controls the random seed in bootstrap sampling.

        Returns
        ----------
        policy_value_interval_dict: Dict[str, Dict[str, float]]
            Dictionary containing confidence intervals of estimated policy value estimated
            using nonparametric bootstrap procedure.

        """
        check_confidence_interval_arguments(
            alpha=alpha,
            n_bootstrap_samples=n_bootstrap_samples,
            random_state=random_state,
        )
        policy_value_interval_dict = dict()
        estimator_inputs = self._create_estimator_inputs(
            evaluation_policy_pscore=evaluation_policy_pscore,
            evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
            evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
            evaluation_policy_action_dist=evaluation_policy_action_dist,
        )
        for estimator_name, estimator in self.ope_estimators_.items():
            policy_value_interval_dict[estimator_name] = estimator.estimate_interval(
                **estimator_inputs,
                alpha=alpha,
                n_bootstrap_samples=n_bootstrap_samples,
                random_state=random_state,
            )

        return policy_value_interval_dict

    def summarize_off_policy_estimates(
        self,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
        alpha: float = 0.05,
        n_bootstrap_samples: int = 100,
        random_state: Optional[int] = None,
    ) -> Tuple[DataFrame, DataFrame]:
        """Summarize policy values estimated by OPE estimators and their confidence intervals estimated by a nonparametric bootstrap procedure.

        Parameters
        ------------
        evaluation_policy_pscore: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities of evaluation policy, i.e., :math:`\\pi_e(a_t|x_t)`.

        evaluation_policy_pscore_item_position: array-like, shape (<= n_rounds * len_list,)
            Marginal action choice probabilities of the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(a_{t, k}|x_t)`.

        evaluation_policy_pscore_cascade: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities above the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(\\{a_{t, j}\\}_{j \\le k}|x_t)`.

        evaluation_policy_action_dist: array-like, shape (n_rounds * len_list * n_unique_action, )
            Action choice probabilities of evaluation policy for all possible actions
            , i.e., :math:`\\pi_e({a'}_t(k) | x_t, a_t(1), \\ldots, a_t(k-1)) \\forall {a'}_t(k) \\in \\mathcal{A}`.

        alpha: float, default=0.05
            Significance level.

        n_bootstrap_samples: int, default=100
            Number of resampling performed in the bootstrap procedure.

        random_state: int, default=None
            Controls the random seed in bootstrap sampling.

        Returns
        ----------
        (policy_value_df, policy_value_interval_df): Tuple[DataFrame, DataFrame]
            Policy values and their confidence intervals Estimated by OPE estimators.

        """
        policy_value_df = DataFrame(
            self.estimate_policy_values(
                evaluation_policy_pscore=evaluation_policy_pscore,
                evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
                evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
                evaluation_policy_action_dist=evaluation_policy_action_dist,
            ),
            index=["estimated_policy_value"],
        )
        policy_value_interval_df = DataFrame(
            self.estimate_intervals(
                evaluation_policy_pscore=evaluation_policy_pscore,
                evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
                evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
                evaluation_policy_action_dist=evaluation_policy_action_dist,
                alpha=alpha,
                n_bootstrap_samples=n_bootstrap_samples,
                random_state=random_state,
            )
        )
        policy_value_of_behavior_policy = (
            self.bandit_feedback["reward"].sum()
            / np.unique(self.bandit_feedback["slate_id"]).shape[0]
        )
        policy_value_df = policy_value_df.T
        if policy_value_of_behavior_policy <= 0:
            logger.warning(
                f"Policy value of the behavior policy is {policy_value_of_behavior_policy} (<=0); relative estimated policy value is set to np.nan"
            )
            policy_value_df["relative_estimated_policy_value"] = np.nan
        else:
            policy_value_df["relative_estimated_policy_value"] = (
                policy_value_df.estimated_policy_value / policy_value_of_behavior_policy
            )
        return policy_value_df, policy_value_interval_df.T

    def visualize_off_policy_estimates(
        self,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
        alpha: float = 0.05,
        is_relative: bool = False,
        n_bootstrap_samples: int = 100,
        random_state: Optional[int] = None,
        fig_dir: Optional[Path] = None,
        fig_name: str = "estimated_policy_value.png",
    ) -> None:
        """Visualize policy values estimated by OPE estimators.

        Parameters
        ----------
        evaluation_policy_pscore: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities of evaluation policy, i.e., :math:`\\pi_e(a_t|x_t)`.

        evaluation_policy_pscore_item_position: array-like, shape (<= n_rounds * len_list,)
            Marginal action choice probabilities of the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(a_{t, k}|x_t)`.

        evaluation_policy_pscore_cascade: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities above the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(\\{a_{t, j}\\}_{j \\le k}|x_t)`.

        evaluation_policy_action_dist: array-like, shape (n_rounds * len_list * n_unique_action, )
            Action choice probabilities of evaluation policy for all possible actions
            , i.e., :math:`\\pi_e({a'}_t(k) | x_t, a_t(1), \\ldots, a_t(k-1)) \\forall {a'}_t(k) \\in \\mathcal{A}`.

        alpha: float, default=0.05
            Significance level.

        n_bootstrap_samples: int, default=100
            Number of resampling performed in the bootstrap procedure.

        random_state: int, default=None
            Controls the random seed in bootstrap sampling.

        is_relative: bool, default=False,
            If True, the method visualizes the estimated policy values of evaluation policy
            relative to the ground-truth policy value of behavior policy.

        fig_dir: Path, default=None
            Path to store the bar figure.
            If 'None' is given, the figure will not be saved.

        fig_name: str, default="estimated_policy_value.png"
            Name of the bar figure.

        """
        if fig_dir is not None:
            assert isinstance(fig_dir, Path), "fig_dir must be a Path"
        if fig_name is not None:
            assert isinstance(fig_name, str), "fig_dir must be a string"

        _, estimated_interval_a = self.summarize_off_policy_estimates(
            evaluation_policy_pscore=evaluation_policy_pscore,
            evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
            evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
            evaluation_policy_action_dist=evaluation_policy_action_dist,
            alpha=alpha,
            n_bootstrap_samples=n_bootstrap_samples,
            random_state=random_state,
        )
        estimated_interval_a["errbar_length"] = (
            estimated_interval_a.drop("mean", axis=1).diff(axis=1).iloc[:, -1].abs()
        )
        if is_relative:
            estimated_interval_a /= (
                self.bandit_feedback["reward"].sum()
                / np.unique(self.bandit_feedback["slate_id"]).shape[0]
            )

        plt.style.use("ggplot")
        fig, ax = plt.subplots(figsize=(8, 6))
        sns.barplot(
            data=estimated_interval_a[["mean"]].reset_index(),
            x="index",
            y="mean",
            ax=ax,
            ci=None,
        )
        plt.xlabel("OPE Estimators", fontsize=25)
        plt.ylabel(
            f"Estimated Policy Value (± {np.int(100*(1 - alpha))}% CI)", fontsize=20
        )
        plt.yticks(fontsize=15)
        plt.xticks(fontsize=25 - 2 * len(self.ope_estimators))
        ax.errorbar(
            np.arange(estimated_interval_a.shape[0]),
            estimated_interval_a["mean"],
            yerr=estimated_interval_a["errbar_length"],
            fmt="o",
            color="black",
        )

        if fig_dir:
            fig.savefig(str(fig_dir / fig_name))

    def evaluate_performance_of_estimators(
        self,
        ground_truth_policy_value: float,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
        metric: str = "relative-ee",
    ) -> Dict[str, float]:
        """Evaluate estimation performance of OPE estimators.

        Note
        ------
        Evaluate the estimation performance of OPE estimators by relative estimation error (relative-EE) or squared error (SE):

        .. math ::

            \\text{Relative-EE} (\\hat{V}; \\mathcal{D}) = \\left|  \\frac{\\hat{V}(\\pi; \\mathcal{D}) - V(\\pi)}{V(\\pi)} \\right|,

        .. math ::

            \\text{SE} (\\hat{V}; \\mathcal{D}) = \\left(\\hat{V}(\\pi; \\mathcal{D}) - V(\\pi) \\right)^2,

        where :math:`V({\\pi})` is the ground-truth policy value of the evalation policy :math:`\\pi_e` (often estimated using on-policy estimation).
        :math:`\\hat{V}(\\pi; \\mathcal{D})` is an estimated policy value by an OPE estimator :math:`\\hat{V}` and logged bandit feedback :math:`\\mathcal{D}`.

        Parameters
        ----------
        ground_truth_policy_value: float
            Ground_truth policy value of evaluation policy, i.e., :math:`V(\\pi)`.
            With Open Bandit Dataset, in general, we use an on-policy estimate of the policy value as its ground-truth.

        evaluation_policy_pscore: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities of evaluation policy, i.e., :math:`\\pi_e(a_t|x_t)`.

        evaluation_policy_pscore_item_position: array-like, shape (<= n_rounds * len_list,)
            Marginal action choice probabilities of the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(a_{t, k}|x_t)`.

        evaluation_policy_pscore_cascade: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities above the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(\\{a_{t, j}\\}_{j \\le k}|x_t)`.

        evaluation_policy_action_dist: array-like, shape (n_rounds * len_list * n_unique_action, )
            Action choice probabilities of evaluation policy for all possible actions
            , i.e., :math:`\\pi_e({a'}_t(k) | x_t, a_t(1), \\ldots, a_t(k-1)) \\forall {a'}_t(k) \\in \\mathcal{A}`.

        metric: str, default="relative-ee"
            Evaluation metric used to evaluate and compare the estimation performance of OPE estimators.
            Must be "relative-ee" or "se".

        Returns
        ----------
        eval_metric_ope_dict: Dict[str, float]
            Dictionary containing evaluation metric for evaluating the estimation performance of OPE estimators.

        """
        check_scalar(ground_truth_policy_value, "ground_truth_policy_value", float)
        if metric not in ["relative-ee", "se"]:
            raise ValueError(
                f"metric must be either 'relative-ee' or 'se', but {metric} is given"
            )
        if metric == "relative-ee" and ground_truth_policy_value == 0.0:
            raise ValueError(
                "ground_truth_policy_value must be non-zero when metric is relative-ee"
            )

        eval_metric_ope_dict = dict()
        estimator_inputs = self._create_estimator_inputs(
            evaluation_policy_pscore=evaluation_policy_pscore,
            evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
            evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
            evaluation_policy_action_dist=evaluation_policy_action_dist,
        )
        for estimator_name, estimator in self.ope_estimators_.items():
            estimated_policy_value = estimator.estimate_policy_value(**estimator_inputs)
            if metric == "relative-ee":
                relative_ee_ = estimated_policy_value - ground_truth_policy_value
                relative_ee_ /= ground_truth_policy_value
                eval_metric_ope_dict[estimator_name] = np.abs(relative_ee_)
            elif metric == "se":
                se_ = (estimated_policy_value - ground_truth_policy_value) ** 2
                eval_metric_ope_dict[estimator_name] = se_
        return eval_metric_ope_dict

    def summarize_estimators_comparison(
        self,
        ground_truth_policy_value: float,
        evaluation_policy_pscore: Optional[np.ndarray] = None,
        evaluation_policy_pscore_item_position: Optional[np.ndarray] = None,
        evaluation_policy_pscore_cascade: Optional[np.ndarray] = None,
        evaluation_policy_action_dist: Optional[np.ndarray] = None,
        metric: str = "relative-ee",
    ) -> DataFrame:
        """Summarize performance comparisons of OPE estimators.

        Parameters
        ----------
        ground_truth_policy_value: float
            Ground_truth policy value of evaluation policy, i.e., :math:`V(\\pi)`.
            With Open Bandit Dataset, in general, we use an on-policy estimate of the policy value as ground-truth.

        evaluation_policy_pscore: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities of evaluation policy, i.e., :math:`\\pi_e(a_t|x_t)`.

        evaluation_policy_pscore_item_position: array-like, shape (<= n_rounds * len_list,)
            Marginal action choice probabilities of the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(a_{t, k}|x_t)`.

        evaluation_policy_pscore_cascade: array-like, shape (<= n_rounds * len_list,)
            Action choice probabilities above the slot (:math:`k`) by the evaluation policy, i.e., :math:`\\pi_e(\\{a_{t, j}\\}_{j \\le k}|x_t)`.

        evaluation_policy_action_dist: array-like, shape (n_rounds * len_list * n_unique_action, )
            Action choice probabilities of evaluation policy for all possible actions
            , i.e., :math:`\\pi_e({a'}_t(k) | x_t, a_t(1), \\ldots, a_t(k-1)) \\forall {a'}_t(k) \\in \\mathcal{A}`.

        metric: str, default="relative-ee"
            Evaluation metric used to evaluate and compare the estimation performance of OPE estimators.
            Must be either "relative-ee" or "se".

        Returns
        ----------
        eval_metric_ope_df: DataFrame
            Evaluation metric to evaluate and compare the estimation performance of OPE estimators.

        """
        eval_metric_ope_df = DataFrame(
            self.evaluate_performance_of_estimators(
                ground_truth_policy_value=ground_truth_policy_value,
                evaluation_policy_pscore=evaluation_policy_pscore,
                evaluation_policy_pscore_item_position=evaluation_policy_pscore_item_position,
                evaluation_policy_pscore_cascade=evaluation_policy_pscore_cascade,
                metric=metric,
            ),
            index=[metric],
        )
        return eval_metric_ope_df.T
