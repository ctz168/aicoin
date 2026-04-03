#!/usr/bin/env python3
"""
Comprehensive test suite for AICoin project.
All tests use simulation mode (no real blockchain required).
"""

import sys
import os
import json
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.blockchain import (
    BlockchainManager,
    ProposalStatus as BCProposalStatus,
    ProposalType as BCProposalType,
    MiningInfo,
    InsufficientBalanceError,
    MiningError,
    TOKEN_DECIMALS_FACTOR,
    INITIAL_BLOCK_REWARD,
    HALVING_INTERVAL,
    API_PRICES,
)

from core.mining_engine import (
    ComputeMeter,
    MiningEngine,
    MiningState,
    MiningConfig,
    MINING_CONSTANTS,
    ComputeProof,
    RewardDistributor,
)

from core.router import (
    NodeRegistry,
    OptimalRouter,
    LatencyProbe,
    RoutingConfig,
    RoutingStrategy,
    NodeStatus,
    RequestTracker,
)

from core.governance import (
    GovernanceManager,
    ModelRegistry,
    ProposalExecutor,
    Proposal as GovProposal,
    ProposalStatus as GovProposalStatus,
    ProposalType as GovProposalType,
    MIN_PROPOSAL_STAKE,
    APPROVAL_THRESHOLD,
    QUORUM_RATIO,
)

from core.config import AICoinConfig


# ======================================================================
# Helper: create a fresh BlockchainManager in simulation mode
# ======================================================================

def _make_bm(config_overrides=None):
    cfg = {"mode": "simulation", "auto_save": False}
    if config_overrides:
        cfg.update(config_overrides)
    return BlockchainManager(cfg)


# ======================================================================
# 1. BLOCKCHAIN TESTS
# ======================================================================

class TestBlockchainBalance(unittest.TestCase):
    """Tests for get_balance, transfer, mint, burn."""

    def setUp(self):
        self.bm = _make_bm()

    def test_get_balance_default_zero(self):
        self.assertEqual(self.bm.get_balance("0xNewUser"), 0)

    def test_mint_mining_reward(self):
        self.bm.mint_mining_reward("0xMiner1", 100 * TOKEN_DECIMALS_FACTOR)
        self.assertEqual(self.bm.get_balance("0xMiner1"), 100 * TOKEN_DECIMALS_FACTOR)

    def test_mint_mining_reward_invalid_amount(self):
        with self.assertRaises(ValueError):
            self.bm.mint_mining_reward("0xM1", 0)
        with self.assertRaises(ValueError):
            self.bm.mint_mining_reward("0xM1", -1)

    def test_transfer_success(self):
        self.bm.mint_mining_reward("0xAlice", 100 * TOKEN_DECIMALS_FACTOR)
        result = self.bm.transfer("0xAlice", "0xBob", 30 * TOKEN_DECIMALS_FACTOR)
        self.assertTrue(result)
        self.assertEqual(self.bm.get_balance("0xAlice"), 70 * TOKEN_DECIMALS_FACTOR)
        self.assertEqual(self.bm.get_balance("0xBob"), 30 * TOKEN_DECIMALS_FACTOR)

    def test_transfer_insufficient_balance(self):
        self.bm.mint_mining_reward("0xAlice", 10 * TOKEN_DECIMALS_FACTOR)
        with self.assertRaises(InsufficientBalanceError):
            self.bm.transfer("0xAlice", "0xBob", 20 * TOKEN_DECIMALS_FACTOR)

    def test_transfer_invalid_amount(self):
        self.bm.mint_mining_reward("0xAlice", 100 * TOKEN_DECIMALS_FACTOR)
        with self.assertRaises(ValueError):
            self.bm.transfer("0xAlice", "0xBob", 0)
        with self.assertRaises(ValueError):
            self.bm.transfer("0xAlice", "0xBob", -5)

    def test_burn_tokens_success(self):
        self.bm.mint_mining_reward("0xAlice", 100 * TOKEN_DECIMALS_FACTOR)
        result = self.bm.burn_tokens("0xAlice", 40 * TOKEN_DECIMALS_FACTOR, "API access")
        self.assertTrue(result)
        self.assertEqual(self.bm.get_balance("0xAlice"), 60 * TOKEN_DECIMALS_FACTOR)

    def test_burn_tokens_insufficient_balance(self):
        self.bm.mint_mining_reward("0xAlice", 10 * TOKEN_DECIMALS_FACTOR)
        with self.assertRaises(InsufficientBalanceError):
            self.bm.burn_tokens("0xAlice", 50 * TOKEN_DECIMALS_FACTOR)

    def test_burn_tokens_invalid_amount(self):
        self.bm.mint_mining_reward("0xAlice", 100 * TOKEN_DECIMALS_FACTOR)
        with self.assertRaises(ValueError):
            self.bm.burn_tokens("0xAlice", 0)

    def test_total_supply_tracking(self):
        self.bm.mint_mining_reward("0xM1", 50 * TOKEN_DECIMALS_FACTOR)
        self.bm.mint_mining_reward("0xM2", 30 * TOKEN_DECIMALS_FACTOR)
        self.assertEqual(self.bm._total_supply, 80 * TOKEN_DECIMALS_FACTOR)
        self.bm.burn_tokens("0xM1", 10 * TOKEN_DECIMALS_FACTOR)
        self.assertEqual(self.bm._total_supply, 70 * TOKEN_DECIMALS_FACTOR)


class TestBlockchainMining(unittest.TestCase):
    """Tests for mining functions."""

    def setUp(self):
        self.bm = _make_bm()

    def _make_proof(self):
        return {
            "task_hash": "0xabc123",
            "result_hash": "0xdef456",
            "timestamp": time.time(),
            "signature": "0xsig",
        }

    def test_submit_compute_proof_success(self):
        proof = self._make_proof()
        result = self.bm.submit_compute_proof("0xNode1", 100.0, 5, proof)
        self.assertTrue(result)
        self.assertEqual(self.bm._mining_info["0xnode1"].tasks_completed, 5)
        self.assertAlmostEqual(self.bm._mining_info["0xnode1"].compute_power, 100.0)

    def test_submit_compute_proof_incomplete_data(self):
        bad_proof = {"task_hash": "0xabc", "result_hash": "0xdef"}
        with self.assertRaises(MiningError):
            self.bm.submit_compute_proof("0xNode1", 100.0, 1, bad_proof)

    def test_submit_compute_proof_negative_tasks(self):
        proof = self._make_proof()
        with self.assertRaises(ValueError):
            self.bm.submit_compute_proof("0xNode1", 100.0, -1, proof)

    def test_get_total_network_power(self):
        proof = self._make_proof()
        self.bm.submit_compute_proof("0xN1", 200.0, 1, dict(proof))
        self.bm.submit_compute_proof("0xN2", 300.0, 1, dict(proof))
        power = self.bm.get_total_network_power()
        self.assertAlmostEqual(power, 500.0, places=1)

    def test_claim_mining_reward(self):
        proof = self._make_proof()
        self.bm.submit_compute_proof("0xNode1", 100.0, 10, proof)
        # Manually set pending reward and ensure block > 0
        info = self.bm._mining_info["0xnode1"]
        info.pending_reward = 50 * TOKEN_DECIMALS_FACTOR
        info.last_proof_block = 1
        info.compute_power = 100.0
        claimed = self.bm.claim_mining_reward("0xNode1")
        self.assertGreater(claimed, 0)
        # Verify balance increased
        bal = self.bm.get_balance("0xNode1")
        self.assertGreater(bal, 0)

    def test_get_pending_reward(self):
        proof = self._make_proof()
        self.bm.submit_compute_proof("0xNode1", 100.0, 5, proof)
        pending = self.bm.get_pending_reward("0xNode1")
        self.assertIsInstance(pending, int)


class TestBlockchainGovernance(unittest.TestCase):
    """Tests for blockchain proposal functions."""

    def setUp(self):
        self.bm = _make_bm()

    def test_create_proposal(self):
        pid = self.bm.create_proposal(
            proposer="0xProposer",
            title="Test Proposal",
            description="A test proposal",
            proposal_type=BCProposalType.PARAMETER_CHANGE.value,
        )
        self.assertIsInstance(pid, int)
        proposal = self.bm.get_proposal(pid)
        self.assertIsNotNone(proposal)
        # get_proposal returns a dict
        self.assertEqual(proposal["title"], "Test Proposal")
        self.assertEqual(proposal["proposer"], "0xproposer")

    def test_vote_on_proposal(self):
        pid = self.bm.create_proposal(
            proposer="0xP1",
            title="Vote Test",
            description="Testing votes",
            proposal_type=BCProposalType.PARAMETER_CHANGE.value,
        )
        # Mint tokens to a voter
        self.bm.mint_mining_reward("0xVoter1", 1000 * TOKEN_DECIMALS_FACTOR)
        result = self.bm.vote("0xVoter1", pid, True)
        self.assertTrue(result)

    def test_vote_double_vote_fails(self):
        pid = self.bm.create_proposal(
            proposer="0xP1", title="Double", description="d",
            proposal_type=BCProposalType.PARAMETER_CHANGE.value,
        )
        self.bm.mint_mining_reward("0xV", 500 * TOKEN_DECIMALS_FACTOR)
        self.bm.vote("0xV", pid, True)
        with self.assertRaises(Exception):
            self.bm.vote("0xV", pid, False)

    def test_get_proposal_nonexistent(self):
        with self.assertRaises(Exception):
            self.bm.get_proposal(99999)

    def test_get_active_proposals(self):
        self.bm.create_proposal(
            proposer="0xP1", title="A1", description="d",
            proposal_type=BCProposalType.PARAMETER_CHANGE.value,
        )
        self.bm.create_proposal(
            proposer="0xP1", title="A2", description="d",
            proposal_type=BCProposalType.PARAMETER_CHANGE.value,
        )
        active = self.bm.get_active_proposals()
        self.assertGreaterEqual(len(active), 2)


class TestBlockchainAPIAccess(unittest.TestCase):
    """Tests for API access burn functions."""

    def setUp(self):
        self.bm = _make_bm()

    def test_get_api_price(self):
        basic = self.bm.get_api_price("basic")
        self.assertEqual(basic, API_PRICES["basic"])
        premium = self.bm.get_api_price("premium")
        self.assertEqual(premium, API_PRICES["premium"])

    def test_burn_for_api_access(self):
        self.bm.mint_mining_reward("0xUser1", 100 * TOKEN_DECIMALS_FACTOR)
        result = self.bm.burn_for_api_access(
            "0xUser1", API_PRICES["basic"], tier="basic",
        )
        self.assertTrue(result)
        # Verify access
        check = self.bm.check_access("0xUser1")
        self.assertTrue(check["allowed"])

    def test_burn_for_api_access_insufficient(self):
        self.bm.mint_mining_reward("0xUser1", 1)  # tiny amount
        with self.assertRaises(InsufficientBalanceError):
            self.bm.burn_for_api_access(
                "0xUser1", API_PRICES["premium"], tier="premium",
            )

    def test_check_access_no_tokens(self):
        check = self.bm.check_access("0xNobody")
        self.assertFalse(check["allowed"])


class TestBlockchainInfo(unittest.TestCase):
    """Tests for blockchain info queries."""

    def setUp(self):
        self.bm = _make_bm()

    def test_get_current_block(self):
        block = self.bm.get_current_block()
        self.assertIsInstance(block, int)
        self.assertGreaterEqual(block, 0)

    def test_get_halving_info(self):
        info = self.bm.get_halving_info()
        self.assertIn("current_epoch", info)
        self.assertIn("blocks_until_halving", info)
        self.assertIn("current_reward", info)
        self.assertIn("next_reward", info)
        self.assertGreaterEqual(info["current_reward"], 0)

    def test_get_blockchain_stats(self):
        stats = self.bm.get_blockchain_stats()
        self.assertIn("total_supply", stats)
        self.assertIn("burned_total", stats)
        self.assertIn("mining_reward_total", stats)
        self.assertIn("total_network_power", stats)
        self.assertIsInstance(stats["total_supply"], int)

    def test_block_reward_decreases(self):
        r0 = self.bm._get_block_reward(0)
        r1 = self.bm._get_block_reward(HALVING_INTERVAL)
        self.assertGreater(r0, r1)


# ======================================================================
# 2. MINING ENGINE TESTS
# ======================================================================

class TestComputeMeter(unittest.TestCase):
    """Tests for the ComputeMeter class."""

    def setUp(self):
        self.meter = ComputeMeter(node_id="test-node-001")

    def test_record_inference_success(self):
        tid = self.meter.record_inference(
            tokens_in=100, tokens_out=50, inference_time=0.5,
            model_name="qwen2.5-7b", gpu_used=8.0, success=True,
        )
        self.assertIsInstance(tid, str)
        self.assertEqual(self.meter.get_record_count(), 1)

    def test_record_inference_failure(self):
        self.meter.record_inference(
            tokens_in=100, tokens_out=0, inference_time=0.1,
            model_name="qwen2.5-7b", gpu_used=4.0, success=False,
            error_msg="OOM",
        )
        stats = self.meter.get_all_time_stats()
        self.assertEqual(stats["total_tasks"], 1)
        self.assertEqual(stats["failed_tasks"], 1)

    def test_record_negative_tokens_corrected(self):
        self.meter.record_inference(
            tokens_in=-10, tokens_out=-5, inference_time=0.5,
            model_name="test", gpu_used=1.0,
        )
        stats = self.meter.get_all_time_stats()
        self.assertEqual(stats["total_tokens_in"], 0)
        self.assertEqual(stats["total_tokens_out"], 0)

    def test_get_compute_score(self):
        # Record several inferences to build up score
        for _ in range(5):
            self.meter.record_inference(
                tokens_in=500, tokens_out=200, inference_time=0.3,
                model_name="qwen2.5-7b", gpu_used=12.0,
            )
        score = self.meter.get_compute_score()
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)

    def test_generate_proof(self):
        self.meter.record_inference(
            tokens_in=100, tokens_out=50, inference_time=0.2,
            model_name="qwen2.5-7b", gpu_used=8.0,
        )
        proof = self.meter.generate_proof(block_height=42)
        self.assertIsInstance(proof, ComputeProof)
        self.assertEqual(proof.node_id, "test-node-001")
        self.assertEqual(proof.block_height, 42)
        self.assertTrue(len(proof.signature_hash) > 0)
        self.assertEqual(proof.tasks_24h, 1)

    def test_get_hourly_stats(self):
        self.meter.record_inference(
            tokens_in=100, tokens_out=50, inference_time=0.5,
            model_name="qwen2.5-7b", gpu_used=8.0,
        )
        stats = self.meter.get_hourly_stats()
        self.assertIn("period", stats)
        self.assertEqual(stats["period"], "24\u5c0f\u65f6")
        self.assertEqual(stats["total_tasks"], 1)
        self.assertEqual(stats["success_tasks"], 1)
        self.assertIn("completion_rate", stats)

    def test_get_all_time_stats(self):
        self.meter.record_inference(
            tokens_in=200, tokens_out=100, inference_time=1.0,
            model_name="llama-3-8b", gpu_used=16.0,
        )
        stats = self.meter.get_all_time_stats()
        self.assertEqual(stats["total_tasks"], 1)
        self.assertEqual(stats["total_tokens"], 300)
        self.assertIn("top_models", stats)

    def test_get_uptime(self):
        uptime = self.meter.get_uptime_seconds()
        self.assertGreater(uptime, 0.0)

    def test_reset(self):
        self.meter.record_inference(
            tokens_in=100, tokens_out=50, inference_time=0.5,
            model_name="test", gpu_used=4.0,
        )
        self.meter.reset()
        self.assertEqual(self.meter.get_record_count(), 0)
        stats = self.meter.get_all_time_stats()
        self.assertEqual(stats["total_tasks"], 0)


class TestMiningEngine(unittest.TestCase):
    """Tests for the MiningEngine class."""

    def setUp(self):
        self.engine = MiningEngine(
            blockchain_manager=None,
            node_id="miner-001",
        )

    def test_initial_state_idle(self):
        self.assertEqual(self.engine.state, MiningState.IDLE)
        self.assertFalse(self.engine.is_mining)

    def test_start_mining(self):
        self.engine.start_mining()
        self.assertEqual(self.engine.state, MiningState.MINING)
        self.assertTrue(self.engine.is_mining)
        # Cleanup - stop thread (use internal stop event)
        self.engine._stop_event.set()
        self.engine._mining_thread.join(timeout=5)

    def test_stop_mining(self):
        self.engine.start_mining()
        self.engine.stop_mining()
        self.assertFalse(self.engine.is_mining)

    def test_stop_mining_when_not_mining(self):
        # Should not raise
        self.engine.stop_mining()
        # State will be STOPPED after stop attempt on non-mining state
        self.assertIn(self.engine.state, [MiningState.STOPPED, MiningState.IDLE])

    def test_calculate_reward(self):
        # node_power=500, total_power=1000, block_reward=50 => 25
        reward = self.engine.calculate_reward(500.0, 1000.0, 50)
        self.assertEqual(reward, 25)

    def test_calculate_reward_zero_total(self):
        reward = self.engine.calculate_reward(100.0, 0.0, 50)
        self.assertEqual(reward, 0)

    def test_calculate_reward_zero_node(self):
        reward = self.engine.calculate_reward(0.0, 1000.0, 50)
        self.assertEqual(reward, 0)

    def test_get_current_block_reward_epoch0(self):
        reward = self.engine.get_current_block_reward()
        # At epoch 0, reward = 50 // 1 = 50
        self.assertEqual(reward, 50)

    def test_get_halving_countdown(self):
        info = self.engine.get_halving_countdown()
        self.assertIn("current_epoch", info)
        self.assertIn("blocks_remaining", info)
        self.assertIn("current_reward", info)
        self.assertIn("next_reward", info)
        self.assertIn("progress_percent", info)
        self.assertGreaterEqual(info["blocks_remaining"], 0)

    def test_add_and_claim_pending_reward(self):
        self.engine.add_pending_reward(100)
        self.engine.add_pending_reward(200)
        self.assertEqual(self.engine.get_pending_reward(), 300)
        claimed = self.engine.claim_reward()
        self.assertEqual(claimed, 300)
        self.assertEqual(self.engine.get_pending_reward(), 0)
        self.assertEqual(self.engine.get_total_mined(), 300)

    def test_claim_reward_empty(self):
        self.assertEqual(self.engine.claim_reward(), 0)

    def test_add_pending_reward_zero_ignored(self):
        self.engine.add_pending_reward(0)
        self.engine.add_pending_reward(-5)
        self.assertEqual(self.engine.get_pending_reward(), 0)

    def test_meter_accessible(self):
        self.assertIsInstance(self.engine.meter, ComputeMeter)
        self.assertEqual(self.engine.meter.node_id, "miner-001")

    def test_pause_resume_mining(self):
        self.engine.start_mining()
        self.engine.pause_mining()
        self.assertEqual(self.engine.state, MiningState.PAUSED)
        self.engine.resume_mining()
        self.assertEqual(self.engine.state, MiningState.MINING)
        # Cleanup
        self.engine._stop_event.set()
        if self.engine._mining_thread:
            self.engine._mining_thread.join(timeout=5)


class TestRewardDistributor(unittest.TestCase):
    """Tests for the RewardDistributor class."""

    def setUp(self):
        self.rd = RewardDistributor(blockchain_manager=None)

    def test_record_api_revenue(self):
        self.rd.record_api_revenue(node_id="0xNode1", burn_amount=100, compute_power=50.0)
        pool = self.rd.get_pool_balance()
        self.assertGreater(pool['revenue_pool'], 0)

    def test_distribute_revenue(self):
        self.rd.record_api_revenue(node_id="0xN1", burn_amount=100, compute_power=60.0)
        self.rd.record_api_revenue(node_id="0xN2", burn_amount=200, compute_power=40.0)
        result = self.rd.distribute_revenue()
        self.assertIn("total_revenue", result)
        self.assertIn("node_distribution", result)
        self.assertIn("treasury", result)
        self.assertGreater(result["total_revenue"], 0)

    def test_get_distribution_history(self):
        self.rd.record_api_revenue(node_id="0xN1", burn_amount=100, compute_power=50.0)
        self.rd.distribute_revenue()
        history = self.rd.get_distribution_history()
        self.assertGreaterEqual(len(history), 1)


# ======================================================================
# 3. ROUTER TESTS
# ======================================================================

class TestNodeRegistry(unittest.TestCase):
    """Tests for NodeRegistry."""

    def setUp(self):
        self.registry = NodeRegistry()
        # Use long timeout so nodes don't expire during tests
        self.default_node = {
            "id": "node-1",
            "host": "192.168.1.1",
            "port": 5000,
            "compute_score": 80.0,
            "available_models": ["qwen2.5-7b", "llama-3-8b"],
            "geographic_region": "us-west",
        }

    def tearDown(self):
        self.registry.stop_background_cleanup()

    def test_register_node(self):
        result = self.registry.register_node(self.default_node)
        self.assertTrue(result)
        self.assertEqual(self.registry.total_nodes, 1)
        self.assertIn("node-1", self.registry)

    def test_register_node_missing_fields(self):
        result = self.registry.register_node({"id": "bad"})
        self.assertFalse(result)

    def test_unregister_node(self):
        self.registry.register_node(self.default_node)
        result = self.registry.unregister_node("node-1")
        self.assertTrue(result)
        self.assertEqual(self.registry.total_nodes, 0)

    def test_unregister_nonexistent(self):
        result = self.registry.unregister_node("ghost")
        self.assertFalse(result)

    def test_update_heartbeat(self):
        self.registry.register_node(self.default_node)
        self.registry.update_heartbeat("node-1", {
            "current_load": 0.5,
            "concurrent_requests": 10,
        })
        info = self.registry.get_node_info("node-1")
        self.assertAlmostEqual(info["current_load"], 0.5)

    def test_get_alive_nodes(self):
        self.registry.register_node(self.default_node)
        alive = self.registry.get_alive_nodes()
        ids = [n.id for n in alive]
        self.assertIn("node-1", ids)

    def test_get_nodes_by_model(self):
        self.registry.register_node(self.default_node)
        self.registry.register_node({
            "id": "node-2", "host": "10.0.0.1", "port": 5000,
            "available_models": ["llama-3-8b"],
        })
        nodes = self.registry.get_nodes_by_model("qwen2.5-7b")
        ids = [n.id for n in nodes]
        self.assertIn("node-1", ids)
        self.assertNotIn("node-2", ids)

    def test_get_node_info(self):
        self.registry.register_node(self.default_node)
        info = self.registry.get_node_info("node-1")
        self.assertIsNotNone(info)
        self.assertEqual(info["id"], "node-1")

    def test_get_node_info_nonexistent(self):
        self.assertIsNone(self.registry.get_node_info("ghost"))


class TestOptimalRouter(unittest.TestCase):
    """Tests for OptimalRouter."""

    def setUp(self):
        self.registry = NodeRegistry()
        self.probe = LatencyProbe(self.registry)
        # Override probe to return simulated latencies (avoid real TCP)
        self.probe.probe_node = lambda nid: 10.0  # fake 10ms latency
        self.router = OptimalRouter(self.registry, self.probe)

        # Register nodes
        self.router._registry.register_node({
            "id": "node-A", "host": "10.0.0.1", "port": 5000,
            "compute_score": 90.0,
            "available_models": ["qwen2.5-7b"],
            "geographic_region": "us-west",
        })
        self.router._registry.register_node({
            "id": "node-B", "host": "10.0.0.2", "port": 5000,
            "compute_score": 50.0,
            "available_models": ["qwen2.5-7b"],
            "geographic_region": "us-west",
        })

    def tearDown(self):
        self.registry.stop_background_cleanup()
        self.probe.stop_background_probe()

    def test_find_best_node(self):
        best = self.router.find_best_node("qwen2.5-7b")
        self.assertIsNotNone(best)
        self.assertIn(best, ["node-A", "node-B"])

    def test_find_best_node_no_candidates(self):
        best = self.router.find_best_node("nonexistent-model")
        self.assertIsNone(best)

    def test_find_backup_nodes(self):
        backups = self.router.find_backup_nodes("qwen2.5-7b", "node-A", count=1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0], "node-B")

    def test_find_backup_nodes_excludes_primary(self):
        backups = self.router.find_backup_nodes("qwen2.5-7b", "node-A")
        self.assertNotIn("node-A", backups)

    def test_route_with_fallback_success(self):
        result = self.router.route_with_fallback("qwen2.5-7b", {"prompt": "hello"})
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["node_id"])
        self.assertIn("tried_nodes", result)
        self.assertGreater(result["total_latency_ms"], 0)

    def test_route_with_fallback_no_nodes(self):
        result = self.router.route_with_fallback("ghost-model", {"prompt": "hi"})
        self.assertFalse(result["success"])
        self.assertIsNone(result["node_id"])


class TestRequestTracker(unittest.TestCase):
    """Tests for RequestTracker."""

    def setUp(self):
        self.tracker = RequestTracker()

    def test_start_request(self):
        rid = self.tracker.start_request(
            client_id="0xClient1",
            model="qwen2.5-7b",
            tokens_in=100,
            priority="basic",
        )
        self.assertIsInstance(rid, str)

    def test_complete_request(self):
        rid = self.tracker.start_request(
            client_id="0xC1", model="qwen2.5-7b",
            tokens_in=100, tokens_out=200, priority="basic",
        )
        self.tracker.complete_request(rid, tokens_out=200, node_id="node-1")
        billing = self.tracker.get_billing_summary("0xC1")
        self.assertGreater(billing["total_tokens_in"], 0)

    def test_fail_request(self):
        rid = self.tracker.start_request(
            client_id="0xC1", model="qwen2.5-7b",
            tokens_in=100, priority="basic",
        )
        self.tracker.fail_request(rid, reason="timeout")

    def test_get_billing_empty(self):
        billing = self.tracker.get_billing_summary("0xNobody")
        self.assertEqual(billing["total_tokens_in"], 0)


# ======================================================================
# 4. GOVERNANCE TESTS
# ======================================================================

class TestGovernanceManager(unittest.TestCase):
    """Tests for GovernanceManager."""

    def setUp(self):
        self.bc = _make_bm()
        self.bc.mint_mining_reward("0xProposer", 10000 * TOKEN_DECIMALS_FACTOR)
        self.bc.mint_mining_reward("0xVoter1", 5000 * TOKEN_DECIMALS_FACTOR)
        self.bc.mint_mining_reward("0xVoterA", 5000 * TOKEN_DECIMALS_FACTOR)
        self.bc.mint_mining_reward("0xVoterB", 5000 * TOKEN_DECIMALS_FACTOR)
        self.gm = GovernanceManager(blockchain_manager=self.bc)

    def test_create_model_proposal_success(self):
        pid = self.gm.create_model_proposal(
            proposer="0xProposer",
            model_name="Qwen/Qwen2.5-7B-Instruct",
            description="Run 7B model",
        )
        self.assertGreater(pid, 0)

    def test_create_model_proposal_unknown_model(self):
        pid = self.gm.create_model_proposal(
            proposer="0xP", model_name="nonexistent/model",
            description="d",
        )
        self.assertEqual(pid, -1)

    def test_create_param_proposal_success(self):
        pid = self.gm.create_param_proposal(
            proposer="0xProposer", title="Change rate",
            description="d",
            parameters={"api_rate_per_1k_tokens": 0.02},
        )
        self.assertGreater(pid, 0)

    def test_create_param_proposal_empty_params(self):
        pid = self.gm.create_param_proposal(
            proposer="0xP", title="Empty", description="d", parameters={},
        )
        self.assertEqual(pid, -1)

    def test_create_emergency_proposal(self):
        pid = self.gm.create_emergency_proposal(
            proposer="0xProposer",
            title="Emergency pause",
            description="Security issue",
        )
        self.assertGreater(pid, 0)
        proposal = self.gm.get_proposal(pid)
        self.assertEqual(proposal.proposal_type, GovProposalType.EMERGENCY.value)

    def test_vote_success(self):
        pid = self.gm.create_model_proposal(
            "0xProposer", "Qwen/Qwen2.5-7B-Instruct", "d",
        )
        result = self.gm.vote("0xVoter1", pid, True)
        self.assertTrue(result)

    def test_vote_nonexistent_proposal(self):
        result = self.gm.vote("0xVoter1", 99999, True)
        self.assertFalse(result)

    def test_delegate_vote_success(self):
        pid = self.gm.create_model_proposal(
            "0xProposer", "Qwen/Qwen2.5-7B-Instruct", "d",
        )
        result = self.gm.delegate_vote("0xVoterA", "0xVoterB")
        self.assertTrue(result)

    def test_delegate_vote_self_fails(self):
        result = self.gm.delegate_vote("0xSame", "0xSame")
        self.assertFalse(result)

    def test_check_and_execute_proposals(self):
        # No active proposals should not crash
        executed = self.gm.check_and_execute_proposals()
        self.assertIsInstance(executed, list)

    def test_get_proposal(self):
        pid = self.gm.create_model_proposal(
            "0xProposer", "Qwen/Qwen2.5-7B-Instruct", "desc",
        )
        proposal = self.gm.get_proposal(pid)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.id, pid)

    def test_get_active_proposals(self):
        self.gm.create_model_proposal(
            "0xProposer", "Qwen/Qwen2.5-7B-Instruct", "d1",
        )
        self.gm.create_model_proposal(
            "0xProposer", "Qwen/Qwen2.5-0.5B-Instruct", "d2",
        )
        active = self.gm.get_active_proposals()
        self.assertGreaterEqual(len(active), 2)


class TestGovernanceWithBlockchain(unittest.TestCase):
    """Governance tests with a mock blockchain_manager providing balances."""

    def setUp(self):
        self.bc = _make_bm()
        # Give proposer enough stake
        self.bc.mint_mining_reward("0xRich", 10000 * TOKEN_DECIMALS_FACTOR)
        self.bc.mint_mining_reward("0xVoterA", 5000 * TOKEN_DECIMALS_FACTOR)
        self.bc.mint_mining_reward("0xVoterB", 5000 * TOKEN_DECIMALS_FACTOR)
        self.gm = GovernanceManager(blockchain_manager=self.bc)

    def test_create_proposal_with_stake(self):
        pid = self.gm.create_model_proposal(
            proposer="0xRich",
            model_name="Qwen/Qwen2.5-7B-Instruct",
            description="Run 7B model with real stake",
        )
        self.assertGreater(pid, 0)

    def test_vote_with_balance(self):
        pid = self.gm.create_model_proposal(
            proposer="0xRich",
            model_name="Qwen/Qwen2.5-7B-Instruct",
            description="Vote test",
        )
        result = self.gm.vote("0xVoterA", pid, True)
        self.assertTrue(result)
        proposal = self.gm.get_proposal(pid)
        self.assertGreater(proposal.votes_for, 0)

    def test_delegate_vote_with_balances(self):
        result = self.gm.delegate_vote("0xVoterA", "0xVoterB")
        self.assertTrue(result)
        weight = self.gm.get_vote_weight("0xVoterB")
        # VoterB's weight should include VoterA's delegated tokens
        self.assertGreater(weight, 5000 * TOKEN_DECIMALS_FACTOR)

    def test_revoke_delegation(self):
        self.gm.delegate_vote("0xVoterA", "0xVoterB")
        result = self.gm.revoke_delegation("0xVoterA")
        self.assertTrue(result)


class TestModelRegistry(unittest.TestCase):
    """Tests for ModelRegistry."""

    def setUp(self):
        self.mr = ModelRegistry()

    def test_get_active_model(self):
        model = self.mr.get_active_model()
        self.assertEqual(model, "Qwen/Qwen2.5-7B-Instruct")

    def test_set_active_model(self):
        result = self.mr.set_active_model("Qwen/Qwen2.5-0.5B-Instruct")
        self.assertTrue(result)
        self.assertEqual(self.mr.get_active_model(), "Qwen/Qwen2.5-0.5B-Instruct")

    def test_set_active_model_unknown(self):
        result = self.mr.set_active_model("nonexistent/model")
        self.assertFalse(result)

    def test_get_model_info(self):
        info = self.mr.get_model_info("Qwen/Qwen2.5-7B-Instruct")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "Qwen/Qwen2.5-7B-Instruct")
        self.assertIn("min_memory_gb", info)
        self.assertEqual(info["min_gpu_memory_gb"], 8)

    def test_get_model_info_nonexistent(self):
        self.assertIsNone(self.mr.get_model_info("ghost/model"))

    def test_can_network_run_model_sufficient(self):
        resources = {
            "total_memory_gb": 32,
            "total_gpu_memory_gb": 16,
            "total_nodes": 2,
        }
        result = self.mr.can_network_run_model("Qwen/Qwen2.5-7B-Instruct", resources)
        self.assertTrue(result)

    def test_can_network_run_model_insufficient_memory(self):
        resources = {
            "total_memory_gb": 4,
            "total_gpu_memory_gb": 2,
            "total_nodes": 1,
        }
        result = self.mr.can_network_run_model("Qwen/Qwen2.5-7B-Instruct", resources)
        self.assertFalse(result)

    def test_can_network_run_model_nonexistent(self):
        resources = {
            "total_memory_gb": 999,
            "total_gpu_memory_gb": 999,
            "total_nodes": 10,
        }
        result = self.mr.can_network_run_model("ghost/model", resources)
        self.assertFalse(result)

    def test_register_model(self):
        result = self.mr.register_model("custom/model-v1", {
            "min_memory_gb": 8,
            "min_gpu_memory_gb": 4,
            "recommended_nodes": 1,
            "category": "chat",
        })
        self.assertTrue(result)
        info = self.mr.get_model_info("custom/model-v1")
        self.assertIsNotNone(info)

    def test_get_all_models(self):
        models = self.mr.get_all_models()
        self.assertGreater(len(models), 3)

    def test_get_switch_history(self):
        self.mr.set_active_model("Qwen/Qwen2.5-0.5B-Instruct")
        history = self.mr.get_switch_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["to"], "Qwen/Qwen2.5-0.5B-Instruct")


class TestProposalExecutor(unittest.TestCase):
    """Tests for ProposalExecutor."""

    def setUp(self):
        self.executor = ProposalExecutor()

    def test_execute_param_change(self):
        self.executor.register_param_callback("test_param", lambda v: True)
        proposal = GovProposal(
            id=1, proposer="0xP",
            proposal_type=GovProposalType.PARAM_CHANGE.value,
            title="Test", description="d",
            parameters={"test_param": 42},
        )
        result = self.executor.execute(proposal)
        self.assertTrue(result)

    def test_execute_emergency_freeze(self):
        proposal = GovProposal(
            id=2, proposer="0xP",
            proposal_type=GovProposalType.EMERGENCY.value,
            title="Freeze", description="d",
            parameters={"action": "freeze_contracts"},
        )
        result = self.executor.execute(proposal)
        self.assertTrue(result)

    def test_execute_model_switch(self):
        proposal = GovProposal(
            id=3, proposer="0xP",
            proposal_type=GovProposalType.RUN_MODEL.value,
            title="Switch model", description="d",
            model_name="test-model",
        )
        result = self.executor.execute(proposal)
        self.assertTrue(result)

    def test_execute_unknown_action(self):
        proposal = GovProposal(
            id=4, proposer="0xP",
            proposal_type=GovProposalType.EMERGENCY.value,
            title="Bad action", description="d",
            parameters={"action": "unknown_action_xyz"},
        )
        result = self.executor.execute(proposal)
        self.assertFalse(result)

    def test_get_execution_log(self):
        log = self.executor.get_execution_log()
        self.assertIsInstance(log, list)


# ======================================================================
# 5. CONFIG TESTS
# ======================================================================

class TestAICoinConfig(unittest.TestCase):
    """Tests for AICoinConfig."""

    def test_default_values(self):
        config = AICoinConfig()
        self.assertEqual(config.api_port, 8080)
        self.assertEqual(config.p2p_port, 5000)
        self.assertEqual(config.blockchain_mode, "simulation")
        self.assertEqual(config.initial_block_reward, 50)
        self.assertEqual(config.halving_interval, 210000)
        self.assertEqual(config.max_supply, 21000000)
        self.assertEqual(config.log_level, "INFO")
        self.assertTrue(config.auto_mine)
        self.assertTrue(config.api_enabled)

    def test_auto_generates_node_id(self):
        config = AICoinConfig()
        self.assertTrue(len(config.node_id) > 0)

    def test_custom_values(self):
        config = AICoinConfig(api_port=9090, log_level="DEBUG")
        self.assertEqual(config.api_port, 9090)
        self.assertEqual(config.log_level, "DEBUG")

    def test_validation_port_range(self):
        with self.assertRaises(ValueError):
            AICoinConfig(api_port=0)
        with self.assertRaises(ValueError):
            AICoinConfig(p2p_port=99999)

    def test_validation_same_ports(self):
        with self.assertRaises(ValueError):
            AICoinConfig(api_port=5000, p2p_port=5000)

    def test_validation_invalid_blockchain_mode(self):
        with self.assertRaises(ValueError):
            AICoinConfig(blockchain_mode="invalid")

    def test_validation_reward_pct_sum(self):
        with self.assertRaises(ValueError):
            AICoinConfig(node_reward_percentage=90.0, treasury_percentage=20.0)

    def test_validation_invalid_log_level(self):
        with self.assertRaises(ValueError):
            AICoinConfig(log_level="INVALID")

    def test_validation_mining_interval(self):
        with self.assertRaises(ValueError):
            AICoinConfig(mining_interval=0)

    def test_validation_quorum_percentage(self):
        with self.assertRaises(ValueError):
            AICoinConfig(quorum_percentage=0)
        with self.assertRaises(ValueError):
            AICoinConfig(quorum_percentage=101)

    def test_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "api_port": 9999,
                "log_level": "DEBUG",
                "node_name": "test-node",
            }, f)
            tmppath = f.name
        try:
            config = AICoinConfig.from_file(tmppath)
            self.assertEqual(config.api_port, 9999)
            self.assertEqual(config.log_level, "DEBUG")
            self.assertEqual(config.node_name, "test-node")
        finally:
            os.unlink(tmppath)

    def test_from_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            AICoinConfig.from_file("/nonexistent/path/config.json")

    def test_from_file_bad_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{bad json}")
            tmppath = f.name
        try:
            with self.assertRaises(json.JSONDecodeError):
                AICoinConfig.from_file(tmppath)
        finally:
            os.unlink(tmppath)

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            config = AICoinConfig(api_port=7777)
            config.save(path)
            loaded = AICoinConfig.from_file(path)
            self.assertEqual(loaded.api_port, 7777)

    def test_to_dict(self):
        config = AICoinConfig()
        d = config.to_dict()
        self.assertIsInstance(d, dict)
        self.assertIn("api_port", d)
        self.assertIn("blockchain_mode", d)

    def test_to_json(self):
        config = AICoinConfig()
        j = config.to_json()
        parsed = json.loads(j)
        self.assertIn("api_port", parsed)


# ======================================================================
# 6. INTEGRATION TESTS
# ======================================================================

class TestMiningWorkflow(unittest.TestCase):
    """Full mining workflow integration test."""

    def test_full_mining_workflow(self):
        # 1. Create blockchain and mint initial tokens
        bm = _make_bm()
        bm.mint_mining_reward("0xNode1", 10 * TOKEN_DECIMALS_FACTOR)

        # 2. Create mining engine
        engine = MiningEngine(blockchain_manager=bm, node_id="0xNode1")

        # 3. Record inferences (simulating GPU work)
        meter = engine.meter
        for _ in range(10):
            meter.record_inference(
                tokens_in=200, tokens_out=100, inference_time=0.3,
                model_name="qwen2.5-7b", gpu_used=12.0,
            )

        # 4. Generate proof
        proof = meter.generate_proof(block_height=1)
        self.assertIsInstance(proof, ComputeProof)
        self.assertEqual(proof.node_id, "0xNode1")
        self.assertEqual(proof.tasks_24h, 10)
        self.assertGreater(proof.tokens_processed, 0)

        # 5. Verify compute score
        score = meter.get_compute_score()
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.0)

        # 6. Submit proof to blockchain
        proof_data = {
            "task_hash": proof.signature_hash,
            "result_hash": proof.signature_hash,
            "timestamp": proof.timestamp,
            "signature": "0xsig",
        }
        result = bm.submit_compute_proof(
            "0xNode1", proof.compute_power, proof.tasks_24h, proof_data,
        )
        self.assertTrue(result)

        # 7. Verify network power
        power = bm.get_total_network_power()
        self.assertGreater(power, 0.0)

        # 8. Calculate rewards
        reward = engine.calculate_reward(
            node_power=proof.compute_power,
            total_power=power,
            block_reward=50,
        )
        self.assertGreaterEqual(reward, 0)

        # 9. Verify blockchain stats
        stats = bm.get_blockchain_stats()
        self.assertEqual(stats["total_network_power"], power)

        engine.stop_mining()


class TestGovernanceWorkflow(unittest.TestCase):
    """Full governance workflow integration test."""

    def test_full_governance_workflow(self):
        # 1. Setup blockchain with funded accounts
        bm = _make_bm()
        bm.mint_mining_reward("0xProposer", 10000 * TOKEN_DECIMALS_FACTOR)
        bm.mint_mining_reward("0xVoterA", 8000 * TOKEN_DECIMALS_FACTOR)
        bm.mint_mining_reward("0xVoterB", 7000 * TOKEN_DECIMALS_FACTOR)

        # 2. Create governance manager
        gm = GovernanceManager(blockchain_manager=bm)

        # 3. Create a model proposal
        pid = gm.create_model_proposal(
            proposer="0xProposer",
            model_name="Qwen/Qwen2.5-0.5B-Instruct",
            description="Switch to lightweight model for cost savings",
        )
        self.assertGreater(pid, 0)

        # 4. Verify proposal exists
        proposal = gm.get_proposal(pid)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.proposal_type, GovProposalType.RUN_MODEL.value)
        self.assertEqual(proposal.status, GovProposalStatus.ACTIVE.value)
        self.assertEqual(proposal.model_name, "Qwen/Qwen2.5-0.5B-Instruct")

        # 5. Cast votes
        r1 = gm.vote("0xVoterA", pid, True)
        self.assertTrue(r1)
        r2 = gm.vote("0xVoterB", pid, True)
        self.assertTrue(r2)

        # 6. Verify votes recorded
        proposal = gm.get_proposal(pid)
        self.assertGreater(proposal.votes_for, 0)
        self.assertEqual(proposal.voter_count, 2)

        # 7. Verify active proposals list
        active = gm.get_active_proposals()
        self.assertGreaterEqual(len(active), 1)

        # 8. Double vote should fail
        r3 = gm.vote("0xVoterA", pid, False)
        self.assertFalse(r3)

        # 9. Test delegation
        del_result = gm.delegate_vote("0xVoterA", "0xVoterB")
        self.assertTrue(del_result)
        weight = gm.get_vote_weight("0xVoterB")
        # Should include VoterA's delegated weight
        self.assertGreaterEqual(weight, 7000 * TOKEN_DECIMALS_FACTOR)

        # 10. Test model registry
        mr = gm._model_registry
        self.assertIsNotNone(mr.get_model_info("Qwen/Qwen2.5-0.5B-Instruct"))
        self.assertTrue(mr.can_network_run_model("Qwen/Qwen2.5-0.5B-Instruct", {
            "total_memory_gb": 8,
            "total_gpu_memory_gb": 0,
            "total_nodes": 1,
        }))

        # 11. Revoke delegation
        rev = gm.revoke_delegation("0xVoterA")
        self.assertTrue(rev)

        # 12. Check network params
        params = gm.get_network_params()
        self.assertIn("api_rate_per_1k_tokens", params)
        self.assertIn("block_reward", params)


class TestBlockchainPersistence(unittest.TestCase):
    """Test state save/load for simulation mode."""

    def test_save_and_load_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            # Create blockchain, mint tokens, submit proof
            bm1 = _make_bm({"state_file": state_file})
            bm1.mint_mining_reward("0xAlice", 50 * TOKEN_DECIMALS_FACTOR)
            proof_data = {
                "task_hash": "0xabc", "result_hash": "0xdef",
                "timestamp": time.time(), "signature": "0xsig",
            }
            bm1.submit_compute_proof("0xNode1", 100.0, 5, proof_data)
            bm1.save_state()

            # Load state into a new instance
            bm2 = _make_bm({"state_file": state_file})
            self.assertEqual(bm2.get_balance("0xalice"), 50 * TOKEN_DECIMALS_FACTOR)
            self.assertAlmostEqual(bm2.get_total_network_power(), 100.0, places=1)


# ======================================================================
# ENTRY POINT
# ======================================================================

if __name__ == "__main__":
    unittest.main()
