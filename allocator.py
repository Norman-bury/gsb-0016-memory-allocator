#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1MB 字节池上的 first-fit 内存分配器。

内存布局（块大小 8 字节对齐）：

    偏移 0  : magic   (uint32) = 0xA110CA7E
    偏移 4  : size    (uint32) = 块总字节数（含头，payload 也算在里面）
    偏移 8  : free    (uint8)  = 1 空闲 / 0 已分配
    偏移 9..15         保留
    偏移 16 : payload 起点（alloc 的返回值）

空闲块额外在 payload 区里挂链表指针（地址序双向链表）：
    payload[0:4]  前驱空闲块头偏移，NONE=0xFFFFFFFF
    payload[4:8]  后继空闲块头偏移
    payload 末尾 4 字节存 footer = 本块头偏移，
    用来在 free 时 O(1) 找到物理上一块；读出后还会严格校验
    （magic / free 标志 / prev_hdr + prev_size == cur_hdr），防止误判。
"""

import random
import struct

POOL_SIZE = 1024 * 1024

HDR_SIZE = 16          # 头大小，payload 16 字节对齐
ALIGN = 8
MIN_PAYLOAD = 16       # 空闲块至少放得下 prev/next（8B）+ footer（4B），按 8 对齐取 16
MIN_BLOCK = HDR_SIZE + MIN_PAYLOAD  # 32

MAGIC = 0xA110CA7E
FOOT_MAGIC = 0xF007ED  # 不单独使用，footer 只存头偏移，靠交叉校验确认
NONE = 0xFFFFFFFF

pack32 = struct.Struct("<I").pack
unpack32 = struct.Struct("<I").unpack

pool = bytearray(POOL_SIZE)


def _w32(off, value):
    pool[off:off + 4] = pack32(value & 0xFFFFFFFF)


def _r32(off):
    return unpack32(pool[off:off + 4])[0]


def _set_header(off, size, free):
    _w32(off, MAGIC)
    _w32(off + 4, size)
    pool[off + 8] = 1 if free else 0


def _fl_prev(off):
    v = _r32(off + HDR_SIZE)
    return -1 if v == NONE else v


def _fl_next(off):
    v = _r32(off + HDR_SIZE + 4)
    return -1 if v == NONE else v


def _fl_set_prev(off, p):
    _w32(off + HDR_SIZE, NONE if p == -1 else p)


def _fl_set_next(off, n):
    _w32(off + HDR_SIZE + 4, NONE if n == -1 else n)


def _write_footer(off, size):
    # footer 放在块末尾 4 字节，记录本块头偏移
    _w32(off + size - 4, off)


def _init_pool():
    _set_header(0, POOL_SIZE, True)
    _fl_set_prev(0, -1)
    _fl_set_next(0, -1)
    _write_footer(0, POOL_SIZE)
    return 0


_free_head = _init_pool()


def _align_up(n, a):
    return (n + a - 1) & ~(a - 1)


def alloc(size):
    """分配 size 字节，返回 payload 起始偏移；空间不足返回 None。"""
    if size is None or size < 0:
        return None
    need = _align_up(HDR_SIZE + size, ALIGN)
    if need < MIN_BLOCK:
        need = MIN_BLOCK
    if need > POOL_SIZE:
        return None

    global _free_head
    blk = _free_head
    while blk != -1:
        bsize = _r32(blk + 4)
        if bsize >= need:
            nxt = _fl_next(blk)
            prv = _fl_prev(blk)
            remainder = bsize - need

            if remainder >= MIN_BLOCK:
                # 分裂：前 need 字节分给用户，剩余留在空闲链表
                _set_header(blk, need, False)

                free_blk = blk + need
                _set_header(free_blk, remainder, True)
                _fl_set_prev(free_blk, prv)
                _fl_set_next(free_blk, nxt)
                _write_footer(free_blk, remainder)
                if prv != -1:
                    _fl_set_next(prv, free_blk)
                else:
                    _free_head = free_blk
                if nxt != -1:
                    _fl_set_prev(nxt, free_blk)
            else:
                # 残余太小不值得分裂，整块交出
                need = bsize
                _set_header(blk, bsize, False)
                if prv != -1:
                    _fl_set_next(prv, nxt)
                else:
                    _free_head = nxt
                if nxt != -1:
                    _fl_set_prev(nxt, prv)

            return blk + HDR_SIZE

        blk = _fl_next(blk)

    return None


def _find_prev_free_physically(hdr):
    """通过 hdr 前 4 字节的 footer 候选定位物理上一块，严格交叉校验。"""
    if hdr < 4:
        return -1
    cand = _r32(hdr - 4)
    if cand >= hdr or cand + MIN_BLOCK > hdr:
        return -1
    if _r32(cand) != MAGIC:
        return -1
    if pool[cand + 8] != 1:
        return -1
    if cand + _r32(cand + 4) != hdr:
        return -1
    return cand


def free(addr):
    """释放 alloc 返回的 payload 偏移。"""
    global _free_head

    if not isinstance(addr, int) or addr < HDR_SIZE or addr > POOL_SIZE:
        raise ValueError("bad free address: %r" % (addr,))

    hdr = addr - HDR_SIZE
    if _r32(hdr) != MAGIC:
        raise ValueError("bad magic in block header at %d" % hdr)
    if pool[hdr + 8] != 0:
        raise ValueError("double free or corrupt block at %d" % hdr)

    size = _r32(hdr + 4)
    if size < MIN_BLOCK or hdr + size > POOL_SIZE:
        raise ValueError("corrupt block size at %d" % hdr)

    # 标记为空闲并写 footer
    _set_header(hdr, size, True)
    _write_footer(hdr, size)

    # 在地址序空闲链表里找到插入点：prv < hdr < nxt
    prv = -1
    nxt = _free_head
    while nxt != -1 and nxt < hdr:
        prv = nxt
        nxt = _fl_next(nxt)

    # 与物理下一块合并（它若空闲必为链表中的 nxt）
    if nxt != -1 and nxt == hdr + size:
        size += _r32(nxt + 4)
        after = _fl_next(nxt)
        nxt = after
    # （此时先不动 prv 的 next 指针，下面统一插桩）

    # 与物理上一块合并
    if prv != -1 and prv + _r32(prv + 4) == hdr:
        start = prv
        size = _r32(prv + 4) + size
        before = _fl_prev(prv)
    else:
        start = hdr
        before = prv

    # 把合并后的 start 接入链表：before < start < nxt
    _fl_set_prev(start, before)
    _fl_set_next(start, nxt)
    if before != -1:
        _fl_set_next(before, start)
    else:
        _free_head = start
    if nxt != -1:
        _fl_set_prev(nxt, start)

    # 更新头大小与 footer
    _w32(start + 4, size)
    pool[start + 8] = 1
    _write_footer(start, size)


def stats():
    """返回 (occupied, fragmentation, max_free)。"""
    total_free = 0
    max_free = 0
    blk = _free_head
    while blk != -1:
        bsize = _r32(blk + 4)
        total_free += bsize
        if bsize > max_free:
            max_free = bsize
        blk = _fl_next(blk)

    occupied = POOL_SIZE - total_free
    if total_free == 0:
        fragmentation = 0.0
    else:
        fragmentation = 1.0 - max_free / total_free
    return occupied, fragmentation, max_free


# --------------------------------------------------------------------------
# 命令行自测：随机 alloc/free 压力测试
# --------------------------------------------------------------------------

def _self_test(seed=20261008, steps=100000):
    rng = random.Random(seed)
    live = []          # [payload_addr, size, pattern]
    total_live = 0
    max_live = 400000

    def make_pattern(seq, size):
        return bytes((seq + i * 131) & 0xFF for i in range(size))

    # 基础用例
    addrs = []
    for sz in (1, 7, 64, 1000, 50000):
        a = alloc(sz)
        assert a is not None and 0 <= a and a + sz <= POOL_SIZE
        pool[a:a + sz] = b"\xAB" * sz
        addrs.append((a, sz))
    for a, _ in reversed(addrs):
        free(a)
    assert stats() == (0, 0.0, POOL_SIZE), stats()

    seq = 0
    for step in range(steps):
        room = max_live - total_live
        do_free = bool(live) and (room < 256 or rng.random() >= 0.6)
        if do_free:
            idx = rng.randrange(len(live))
            addr, size, pat = live[idx]
            assert pool[addr:addr + size] == pat, "memory corrupted at step %d" % step
            free(addr)
            total_live -= size
            live[idx] = live[-1]
            live.pop()
        else:
            size = rng.randint(64, 512)
            a = alloc(size)
            if a is not None:
                assert 0 <= a and a + size <= POOL_SIZE
                seq += 1
                pat = make_pattern(seq, size)
                pool[a:a + size] = pat
                live.append([a, size, pat])
                total_live += size

        if step % 2000 == 1999:
            ordered = sorted(live, key=lambda x: x[0])
            for i in range(1, len(ordered)):
                pa, ps, _ = ordered[i - 1]
                qa, qs, _ = ordered[i]
                assert pa + ps <= qa, "overlapping allocations at step %d" % step
            for addr, size, pat in live:
                assert pool[addr:addr + size] == pat, "corruption at step %d" % step
            occ, frag, mf = stats()
            assert 0 <= occ <= POOL_SIZE and 0 <= mf <= POOL_SIZE
            assert 0.0 <= frag <= 1.0

    for addr, size, pat in live:
        assert pool[addr:addr + size] == pat
        free(addr)
    assert stats() == (0, 0.0, POOL_SIZE), stats()
    return True


if __name__ == "__main__":
    _self_test()
    print("PASS")
