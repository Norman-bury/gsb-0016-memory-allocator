#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task16（内存分配器）判分脚本。

规则：
1. 交付 allocator.py，暴露：
   - POOL_SIZE：int，池大小 = 1024 * 1024
   - pool：bytearray，长度 POOL_SIZE 的底层字节池
   - alloc(size) -> int，返回 payload 起始偏移（0 <= addr 且 addr + size <= POOL_SIZE），
     没有足够连续空间时返回 None
   - free(addr) -> 释放 addr 指向的块（addr 是 alloc 返回的偏移）
   - stats() -> (occupied, fragmentation, max_free) 三元组
2. 判分脚本跑固定种子的随机 alloc/free 压力测试，检查：
   - 返回地址不越界
   - 同时存活的两块分配区间互不重叠
   - 写入每块的模式串在释放前保持不变（检测越界写 / 重叠分配 / 分裂偏移算错）
   - 全部释放后 stats 回到 (0, 0.0, POOL_SIZE)（检测泄漏 / 合并不完整）
   - 每 2000 步校验一次 stats 返回值范围
3. 另有命令行冒烟测试：`python3 allocator.py` 应能自跑压力测试并退出 0。

用法：
  python3 grade.py
  python3 grade.py --module path/to/allocator.py
"""

import argparse
import importlib.util
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_SIZE = 1024 * 1024


def load_module(path):
    spec = importlib.util.spec_from_file_location("allocator_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_stress(mod, rng):
    """返回 (errors, oom_count)。"""
    pool = mod.pool
    N = POOL_SIZE
    live = []            # [addr, size, pattern]
    total_live = 0
    oom = 0
    errors = []
    seq = 0
    MAX_LIVE = 400000
    STEPS = 100000

    def pattern_for(sq, size):
        return bytes((sq + i * 131) & 0xFF for i in range(size))

    def check_disjoint():
        s = sorted(live, key=lambda x: x[0])
        for i in range(1, len(s)):
            if s[i - 1][0] + s[i - 1][1] > s[i][0]:
                return False
        return True

    # ---- 前置：空池基础分配 ----
    addrs = []
    for sz in (1, 7, 64, 1000, 50000):
        a = mod.alloc(sz)
        if a is None:
            errors.append("空池 alloc(%d) 返回 None" % sz)
        elif not (isinstance(a, int) and 0 <= a and a + sz <= N):
            errors.append("alloc(%d) 返回越界地址 %r" % (sz, a))
        else:
            pool[a:a + sz] = bytes([0xAB]) * sz
            addrs.append((a, sz))
    for a, sz in reversed(addrs):
        try:
            mod.free(a)
        except Exception as e:
            errors.append("前置 free(%d) 抛异常 %s" % (a, e))
            break
    try:
        occ, frag, mf = mod.stats()
        if occ != 0 or mf != N or not (-1e-9 <= frag <= 1e-9):
            errors.append("前置分配全部释放后未回到空状态：%r" % ((occ, frag, mf),))
    except Exception as e:
        errors.append("前置 stats() 抛异常 %s" % e)
    if errors:
        return errors, oom

    # ---- 主压力测试 ----
    for step in range(STEPS):
        room = MAX_LIVE - total_live
        do_free = bool(live) and (room < 256 or rng.random() >= 0.6)
        if do_free:
            idx = rng.randrange(len(live))
            addr, size, pat = live[idx]
            if pool[addr:addr + size] != pat:
                errors.append("第 %d 步：地址 %d 长度 %d 的块内容被改写（越界写或重叠分配）" % (step, addr, size))
            try:
                mod.free(addr)
            except Exception as e:
                errors.append("第 %d 步：free(%d) 抛异常 %s" % (step, addr, e))
            total_live -= size
            live[idx] = live[-1]
            live.pop()
        else:
            size = rng.randint(64, 512)
            a = mod.alloc(size)
            if a is None:
                oom += 1
            elif not (isinstance(a, int) and 0 <= a and a + size <= N):
                errors.append("第 %d 步：alloc(%d) 返回越界地址 %r" % (step, size, a))
                break
            else:
                seq += 1
                pat = pattern_for(seq, size)
                pool[a:a + size] = pat
                live.append([a, size, pat])
                total_live += size

        if step % 2000 == 1999:
            if not check_disjoint():
                errors.append("第 %d 步：检测到重叠的分配区间" % step)
                break
            for addr, size, pat in live:
                if pool[addr:addr + size] != pat:
                    errors.append("第 %d 步：检测到内存被破坏" % step)
                    break
            try:
                occ, frag, mf = mod.stats()
                if not (0 <= occ <= N and 0 <= mf <= N and 0.0 <= frag <= 1.0 + 1e-9):
                    errors.append("第 %d 步：stats 返回异常值 %r" % (step, (occ, frag, mf)))
            except Exception as e:
                errors.append("第 %d 步：stats 抛异常 %s" % (step, e))

    if errors:
        return errors, oom

    # ---- 收尾：全部释放 + 空状态校验 ----
    for addr, size, pat in live:
        if pool[addr:addr + size] != pat:
            errors.append("最终校验：地址 %d 长度 %d 内容被破坏" % (addr, size))
        try:
            mod.free(addr)
        except Exception as e:
            errors.append("最终 free(%d) 抛异常 %s" % (addr, e))
    try:
        occ, frag, mf = mod.stats()
    except Exception as e:
        errors.append("最终 stats() 抛异常 %s" % e)
        return errors, oom
    if occ != 0:
        errors.append("全部释放后 occupied=%r 应为 0（存在泄漏）" % occ)
    if mf != N:
        errors.append("全部释放后 max_free=%r 应为 %d（相邻块合并不完整）" % (mf, N))
    if not (-1e-9 <= frag <= 1e-9):
        errors.append("全部释放后 fragmentation=%r 应为 0" % frag)
    return errors, oom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default=os.path.join(HERE, "allocator.py"))
    args = ap.parse_args()

    print("=" * 60)
    print("判分开始")

    if not os.path.exists(args.module):
        print("[FAIL] 找不到 allocator.py（%s）" % args.module)
        sys.exit(1)

    mod = load_module(args.module)
    for fn in ("alloc", "free", "stats"):
        if not hasattr(mod, fn):
            print("[FAIL] allocator.py 缺少函数 %s" % fn)
            sys.exit(1)
    if not hasattr(mod, "pool") or not hasattr(mod, "POOL_SIZE"):
        print("[FAIL] allocator.py 缺少 pool 或 POOL_SIZE")
        sys.exit(1)
    if len(mod.pool) != POOL_SIZE:
        print("[FAIL] pool 长度 %d，应为 %d" % (len(mod.pool), POOL_SIZE))
        sys.exit(1)

    errors, oom = run_stress(mod, random.Random(20261006))
    if errors:
        for e in errors[:10]:
            print("[FAIL] " + e)
        print("共 %d 个错误" % len(errors))
        print("=" * 60)
        print("总结论： FAIL")
        sys.exit(1)

    print("[OK] 压力测试通过（100000 步，OOM %d 次）" % oom)

    # 命令行冒烟测试
    cli_ok = True
    try:
        r = subprocess.run([sys.executable, args.module],
                           cwd=HERE, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print("[FAIL] 命令行入口退出码非 0：%s" % r.stderr[-300:])
            cli_ok = False
        else:
            print("[OK] 命令行自测入口退出 0")
    except subprocess.TimeoutExpired:
        print("[FAIL] 命令行自测入口超时")
        cli_ok = False
    except Exception as e:
        print("[FAIL] 命令行自测入口异常：%s" % e)
        cli_ok = False

    print("=" * 60)
    print("总结论：", "PASS" if cli_ok else "FAIL")
    sys.exit(0 if cli_ok else 1)


if __name__ == "__main__":
    main()
