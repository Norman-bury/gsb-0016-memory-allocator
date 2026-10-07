#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1MB 字节池上的 first-fit 内存分配器。

内存布局（偏移均相对于块起始）：
    [0:4]  header magic (uint32)
    [4:8]  flags        (uint32, 1 = 已分配, 0 = 空闲)
    [8:12] block size   (uint32, 整块字节数，含头和脚)
    [12 .. size-8)  payload（alloc 返回这里的偏移）
    [size-8:size-4] footer size  (uint32, 与 header 的 size 相同)
    [size-4:size]   footer magic (uint32)

脚的作用：释放某块时，块前面相邻块的位置无法由当前地址直接推出，
但前一块的脚就在 (block_start - 8) 处，读出其大小即可定位前一块头，
从而完成向上合并。

空闲块用按地址有序的双向循环链表管理（_head 为哨兵），
first-fit 从 _head 后继开始找第一块够大的块；够分裂就分裂。
"""

import random
import struct

POOL_SIZE = 1024 * 1024

pool = bytearray(POOL_SIZE)

MAGIC = 0xA110C8ED
FLAG_FREE = 0
FLAG_ALLOC = 1

HDR_SIZE = 12                       # magic + flags + size
FTR_SIZE = 8                        # size + magic
MIN_BLOCK = HDR_SIZE + FTR_SIZE     # 没有 payload 的最小块

_PACK = struct.Struct("<II").pack        # magic/flags + size
_PACK_U32 = struct.Struct("<I").pack
_UNPACK_U32 = struct.Struct("<I").unpack_from

# 空闲链表：哨兵 offset = POOL_SIZE（永不落在池中），双向循环、按地址有序。
_head = POOL_SIZE
_prev = {_head: _head}
_next = {_head: _head}

_occupied = 0         # 所有已分配块占用字节数之和（头 + payload，不含脚）


def _write_header(block, size, flags):
    pool[block:block + 8] = _PACK(MAGIC, flags)
    pool[block + 8:block + 12] = _PACK_U32(size)


def _write_footer(block, size):
    pool[block + size - 8:block + size - 4] = _PACK_U32(size)
    pool[block + size - 4:block + size] = _PACK_U32(MAGIC)


def _read_u32(off):
    return _UNPACK_U32(pool, off)[0]


def _link_insert(block):
    """按地址有序插入空闲双向链表，返回其前驱节点。"""
    succ = _next[_head]
    while succ != _head and succ < block:
        succ = _next[succ]
    pred = _prev[succ]
    _next[pred] = block
    _prev[block] = pred
    _next[block] = succ
    _prev[succ] = block
    return pred


def _link_remove(block):
    p = _prev.pop(block)
    n = _next.pop(block)
    _next[p] = n
    _prev[n] = p


def _init_pool():
    size = POOL_SIZE
    _write_header(0, size, FLAG_FREE)
    _write_footer(0, size)
    _prev.clear()
    _next.clear()
    _prev[_head] = 0
    _next[_head] = 0
    _prev[0] = _head
    _next[0] = _head


_init_pool()


def alloc(size):
    """分配 size 字节，返回 payload 起始偏移；空间不足返回 None。"""
    global _occupied
    if size < 0:
        return None
    need = size + MIN_BLOCK

    block = _next[_head]
    while block != _head:
        bsize = _read_u32(block + 8)
        nxt = _next[block]
        if bsize >= need:
            remainder = bsize - need
            if remainder >= MIN_BLOCK:
                # 分裂：前半段切给本次分配，后半段仍是空闲块。
                _link_remove(block)
                _write_header(block, need, FLAG_ALLOC)
                _write_footer(block, need)
                free_block = block + need
                _write_header(free_block, remainder, FLAG_FREE)
                _write_footer(free_block, remainder)
                # free_block 的地址必然落在 block 与原后继之间，有序插入即可。
                _link_insert(free_block)
                _occupied += need - FTR_SIZE
            else:
                _link_remove(block)
                _write_header(block, bsize, FLAG_ALLOC)
                _write_footer(block, bsize)
                _occupied += bsize - FTR_SIZE
            return block + HDR_SIZE
        block = nxt
    return None


def free(addr):
    """释放 alloc 返回的 payload 偏移。"""
    global _occupied
    block = addr - HDR_SIZE
    if not (0 <= block < POOL_SIZE):
        raise ValueError("非法释放地址: %r" % (addr,))
    if _read_u32(block) != MAGIC:
        raise ValueError("块头 magic 校验失败（地址 %s 不是有效块）" % addr)
    if _read_u32(block + 4) != FLAG_ALLOC:
        raise ValueError("双重释放或释放非已分配块（地址 %s）" % addr)
    size = _read_u32(block + 8)
    if size < MIN_BLOCK or block + size > POOL_SIZE:
        raise ValueError("块大小非法: %r" % size)
    if _read_u32(block + size - 8) != size:
        raise ValueError("块尾 size 校验失败（堆被越界写破坏）")
    if _read_u32(block + size - 4) != MAGIC:
        raise ValueError("块尾 magic 校验失败（堆被越界写破坏）")

    _occupied -= size - FTR_SIZE
    _write_header(block, size, FLAG_FREE)
    _write_footer(block, size)
    pred = _link_insert(block)

    # 向后合并：紧邻的后继若是空闲块。
    succ = _next[block]
    if succ != _head and succ == block + size:
        size += _read_u32(succ + 8)
        _link_remove(succ)
        _write_header(block, size, FLAG_FREE)
        _write_footer(block, size)

    # 向前合并：按地址有序插入后 pred 即前一个空闲块。
    if pred != _head:
        psize = _read_u32(pred + 8)
        if pred + psize == block:
            _link_remove(block)
            size = psize + size
            _write_header(pred, size, FLAG_FREE)
            _write_footer(pred, size)


def stats():
    """返回 (occupied, fragmentation, max_free)。"""
    total_free = POOL_SIZE - _occupied
    max_free = 0
    block = _next[_head]
    while block != _head:
        bsize = _read_u32(block + 8)
        if bsize > max_free:
            max_free = bsize
        block = _next[block]
    if total_free <= 0:
        fragmentation = 0.0
    else:
        fragmentation = 1.0 - max_free / total_free
        if fragmentation < 0.0:
            fragmentation = 0.0
    return _occupied, fragmentation, max_free


# ----------------------- 自测压力测试入口 -----------------------

def _self_test(steps=100000, seed=20261006):
    rng = random.Random(seed)
    live = []
    total_live = 0
    max_live = 400000
    seq = 0

    for step in range(steps):
        room = max_live - total_live
        do_free = bool(live) and (room < 256 or rng.random() >= 0.6)
        if do_free:
            idx = rng.randrange(len(live))
            addr, size, pat = live[idx]
            assert pool[addr:addr + size] == pat, "payload 被破坏"
            free(addr)
            total_live -= size
            live[idx] = live[-1]
            live.pop()
        else:
            size = rng.randint(64, 512)
            addr = alloc(size)
            if addr is not None:
                assert isinstance(addr, int) and 0 <= addr and addr + size <= POOL_SIZE
                seq += 1
                pat = bytes((seq + i * 131) & 0xFF for i in range(size))
                pool[addr:addr + size] = pat
                live.append((addr, size, pat))
                total_live += size

        if step % 2000 == 1999:
            ordered = sorted(live, key=lambda x: x[0])
            for i in range(1, len(ordered)):
                pa, ps, _ = ordered[i - 1]
                ca, _, _ = ordered[i]
                assert pa + ps <= ca, "分配区间重叠"
            occ, frag, mf = stats()
            assert 0 <= occ <= POOL_SIZE and 0 <= mf <= POOL_SIZE
            assert 0.0 <= frag <= 1.0 + 1e-9

    for addr, size, pat in live:
        assert pool[addr:addr + size] == pat, "最终 payload 校验失败"
        free(addr)

    occ, frag, mf = stats()
    assert occ == 0 and mf == POOL_SIZE and abs(frag) <= 1e-9, (occ, frag, mf)

    # 边界用例：0 字节可分配；整池大小必然放不下（块还要头脚）。
    a = alloc(0)
    assert a is not None and 0 <= a < POOL_SIZE
    free(a)
    assert alloc(POOL_SIZE) is None
    assert stats() == (0, 0.0, POOL_SIZE)


if __name__ == "__main__":
    _self_test()
    print("PASS")
