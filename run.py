#!/usr/bin/env python3
"""
AICoin 一键启动脚本
====================
用法:
    python run.py                  # 交互式启动
    python run.py --demo           # 演示模式 (模拟挖矿, 无需GPU)
    python run.py --wallet         # 钱包管理
    python run.py --status         # 查看节点状态
"""

import os
import sys
import time
import json
import argparse

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def demo_mining():
    """演示模式: 模拟完整挖矿流程, 无需GPU或区块链"""
    print()
    print("=" * 60)
    print("  AICoin 挖矿演示 - 模拟模式")
    print("  (无需GPU / 无需区块链 / 本地模拟)")
    print("=" * 60)
    print()

    from core.wallet import AICoinWallet
    from core.blockchain import BlockchainManager
    from core.mining_engine import ComputeMeter, MiningEngine, RewardDistributor

    # ========== 第1步: 创建/加载钱包 ==========
    print("【第1步】 创建钱包...")
    wallet_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(wallet_dir, exist_ok=True)
    wallet = AICoinWallet(os.path.join(wallet_dir, "demo_wallet.dat"))

    wallet_path = os.path.join(wallet_dir, "demo_wallet.dat")
    if os.path.exists(wallet_path):
        wallet.load("demo1234")  # 演示用固定密码
        print(f"  ✅ 钱包已加载")
    else:
        result = wallet.create_new("demo1234")
        print(f"  ✅ 新钱包创建成功!")
    print(f"  📍 钱包地址: {wallet.get_address()}")
    print()

    # ========== 第2步: 初始化区块链 (模拟模式) ==========
    print("【第2步】 初始化区块链 (模拟模式)...")
    blockchain = BlockchainManager({"mode": "simulation", "auto_save": False})
    my_addr = wallet.get_address()

    # 给钱包预充一些 AIC 用于投票质押
    blockchain.mint_mining_reward(my_addr, 5000 * 10**18)
    balance = blockchain.get_balance(my_addr)
    print(f"  ✅ 区块链已连接 (模拟模式)")
    print(f"  💰 钱包余额: {balance / 10**18:.2f} AIC")
    print()

    # ========== 第3步: 启动挖矿引擎 ==========
    print("【第3步】 启动算力挖矿...")
    engine = MiningEngine(
        blockchain_manager=blockchain,
        node_id=my_addr,
    )
    meter = engine.meter
    print(f"  ✅ 挖矿引擎已启动")
    print(f"  ⛏️  当前区块奖励: {engine.get_current_block_reward()} AIC")
    halving = engine.get_halving_countdown()
    print(f"  📉 减半倒计时: {halving['blocks_remaining']} 个区块")
    print()

    # ========== 第4步: 模拟推理任务 (贡献算力) ==========
    print("【第4步】 模拟AI推理任务 (贡献算力挖矿)...")
    print("  正在处理推理请求...")
    print()

    total_tokens = 0
    total_requests = 0

    for i in range(10):
        # 模拟一个推理任务
        tokens_in = 150 + (i * 37) % 200
        tokens_out = 80 + (i * 23) % 150
        inference_time = 0.2 + (i * 0.05)
        gpu_used = 8.0 + (i % 5) * 2.0

        meter.record_inference(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            inference_time=inference_time,
            model_name="Qwen/Qwen2.5-0.5B-Instruct",
            gpu_used=gpu_used,
        )
        total_tokens += tokens_in + tokens_out
        total_requests += 1

        score = meter.get_compute_score()
        bar_len = int(score / 100)
        bar = "█" * bar_len + "░" * (100 - bar_len)
        print(f"  请求 #{i+1:2d} | tokens: {tokens_in+tokens_out:4d} | "
              f"GPU: {gpu_used:.0f}GB | 算力分: [{bar}] {score:.0f}")

    print()
    print(f"  ✅ 完成了 {total_requests} 个推理请求, 共处理 {total_tokens} tokens")
    print()

    # ========== 第5步: 提交算力证明 ==========
    print("【第5步】 提交算力证明到区块链...")
    proof = meter.generate_proof(block_height=1)
    print(f"  📊 算力证明:")
    print(f"     - 算力分数: {proof.compute_power:.2f}")
    print(f"     - 任务数:   {proof.tasks_24h}")
    print(f"     - Token数:  {proof.tokens_processed}")
    print(f"     - GPU时长:  {proof.gpu_hours_24h:.4f} 小时")
    print(f"     - 区块高度: {proof.block_height}")
    print(f"     - 哈希:     {proof.signature_hash[:32]}...")

    result = blockchain.submit_compute_proof(
        node_address=my_addr,
        compute_power=proof.compute_power,
        tasks_completed=proof.tasks_24h,
        proof_data={
            "task_hash": proof.signature_hash,
            "result_hash": proof.signature_hash,
            "timestamp": proof.timestamp,
            "signature": wallet.sign_message(f"proof-{proof.compute_power}"),
        },
    )
    print(f"  ✅ 算力证明已提交: {'成功' if result else '失败'}")
    print()

    # ========== 第6步: 计算挖矿奖励 ==========
    print("【第6步】 计算并领取挖矿奖励...")
    total_power = blockchain.get_total_network_power()

    # claim 待领奖励
    pending = blockchain.get_pending_reward(my_addr)
    if pending > 0:
        claimed = blockchain.claim_mining_reward(my_addr)
        print(f"  💰 领取待领奖励: {claimed / 10**18:.6f} AIC")

    # 铸造新的挖矿奖励
    block_reward = engine.get_current_block_reward()
    reward = engine.calculate_reward(proof.compute_power, total_power, block_reward)
    # calculate_reward 返回的是不含小数的, 转换为 wei 单位
    reward_wei = reward * 10**18
    blockchain.mint_mining_reward(my_addr, reward_wei)
    print(f"  📈 全网总算力:  {total_power:.2f}")
    print(f"  📈 我的算力:    {proof.compute_power:.2f}")
    print(f"  📈 算力占比:    {proof.compute_power/max(total_power,0.01)*100:.2f}%")
    print(f"  💰 本轮挖到:    {reward:.6f} AIC (区块奖励 {block_reward})")
    print()

    # ========== 第7步: 查看最终状态 ==========
    print("=" * 60)
    print("  🏆 挖矿结果")
    print("=" * 60)
    final_balance = blockchain.get_balance(my_addr)
    mining_info = blockchain.get_mining_info(my_addr)
    stats = blockchain.get_blockchain_stats()
    halving = blockchain.get_halving_info()

    print(f"  💰 最终余额:     {final_balance / 10**18:.6f} AIC")
    print(f"  ⛏️  累计挖矿:     {mining_info['claimed_total'] / 10**18:.6f} AIC")
    print(f"  📊 全网总算力:    {stats['total_network_power']:.2f}")
    print(f"  🔗 总供应量:      {stats['total_supply'] / 10**18:.2f} / 21,000,000 AIC")
    print(f"  📉 当前减半周期:  第 {halving['current_epoch']} 轮")
    print(f"  📉 距下次减半:    {halving['blocks_until_halving']} 个区块")
    print(f"  🔗 区块高度:      {stats['current_block']}")
    print(f"  📍 钱包地址:      {my_addr}")
    print()
    print("  💡 提示: 算力贡献越多 → 算力占比越高 → 挖到的 AIC 越多")
    print("  💡 API 被调用时 → 调用者燃烧 AIC → 80% 分给你 → 你就挖到了!")
    print()


def wallet_manager():
    """钱包管理"""
    from core.wallet import CLIWallet
    wallet_dir = os.path.join(os.path.dirname(__file__), "data")
    wallet_file = os.path.join(wallet_dir, "wallet.dat")
    cli = CLIWallet(wallet_file)
    cli.run()


def show_status():
    """显示节点状态"""
    wallet_dir = os.path.join(os.path.dirname(__file__), "data")
    wallet_file = os.path.join(wallet_dir, "wallet.dat")

    if not os.path.exists(wallet_file):
        print("❌ 钱包未创建, 请先运行: python run.py --demo")
        return

    from core.wallet import AICoinWallet
    from core.blockchain import BlockchainManager

    wallet = AICoinWallet(wallet_file)
    pwd = input("输入钱包密码: ")
    if not wallet.load(pwd):
        print("❌ 密码错误")
        return

    state_file = os.path.join(wallet_dir, "state.json")
    if os.path.exists(state_file):
        bc = BlockchainManager({"mode": "simulation", "state_file": state_file})
    else:
        bc = BlockchainManager({"mode": "simulation"})

    addr = wallet.get_address()
    balance = bc.get_balance(addr)
    mining = bc.get_mining_info(addr)
    stats = bc.get_blockchain_stats()
    halving = bc.get_halving_info()

    print()
    print("=" * 50)
    print("  AICoin 节点状态")
    print("=" * 50)
    print(f"  钱包地址:    {addr}")
    print(f"  AIC 余额:    {balance / 10**18:.6f} AIC")
    print(f"  累计挖矿:    {mining['total_claimed'] / 10**18:.6f} AIC")
    print(f"  算力贡献:    {mining['compute_power']:.2f}")
    print(f"  完成任务:    {mining['tasks_completed']}")
    print(f"  待领奖励:    {mining['pending_reward'] / 10**18:.6f} AIC")
    print(f"  全网总算力:  {stats['total_network_power']:.2f}")
    print(f"  总供应量:    {stats['total_supply'] / 10**18:.2f} AIC")
    print(f"  区块高度:    {stats['current_block']}")
    print(f"  减半轮次:    {halving['current_epoch']}")
    print("=" * 50)
    print()


def interactive_start():
    """交互式启动"""
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║          AICoin 节点 v1.0                   ║")
    print("║     去中心化 AI 算力挖矿网络                  ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  1. 演示挖矿 (模拟模式, 无需GPU)            ║")
    print("║  2. 钱包管理 (创建/导入钱包)                  ║")
    print("║  3. 查看状态                                ║")
    print("║  4. 退出                                    ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    choice = input("请选择 (1-4): ").strip()

    if choice == "1":
        demo_mining()
    elif choice == "2":
        wallet_manager()
    elif choice == "3":
        show_status()
    else:
        print("再见!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AICoin 去中心化AI算力挖矿网络")
    parser.add_argument("--demo", action="store_true", help="演示模式: 模拟挖矿流程")
    parser.add_argument("--wallet", action="store_true", help="钱包管理")
    parser.add_argument("--status", action="store_true", help="查看节点状态")

    args = parser.parse_args()

    if args.demo:
        demo_mining()
    elif args.wallet:
        wallet_manager()
    elif args.status:
        show_status()
    else:
        interactive_start()
