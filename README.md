# 内存分配器

## 输入

没有固定输入文件，判分脚本会对你的分配器跑随机 alloc/free 压力测试。压力测试固定种子、10 万步，专门针对内存布局的翻车点：分裂算错偏移、合并漏一边、越界写、双重释放、泄漏。

## 你要交付什么

写一个 `allocator.py`，在 1MB 字节池上实现 first-fit 分配器。模块暴露这些名字：

- `POOL_SIZE`：int，固定为 `1024 * 1024`。
- `pool`：`bytearray`，长度 `POOL_SIZE` 的底层字节池，判分脚本会直接读写它来校验。
- `alloc(size)`：在池里分配 `size` 字节，返回 **payload 起始偏移**（一个 int，`0 <= addr` 且 `addr + size <= POOL_SIZE`）；没有足够连续空间时返回 `None`。
- `free(addr)`：释放 `alloc` 返回的那个偏移，`addr` 指向 payload 起始。
- `stats()`：返回三元组 `(occupied, fragmentation, max_free)`。

每个块带头，头里存块大小和一个 magic 值。空闲块用链表管理，`alloc` 走 first-fit，找到的块够大就要能分裂；`free` 按地址插回空闲链表，还要和前后相邻的空闲块合并。合并是重灾区：只知道下一块的 header 在 `addr + size` 处，上一块的位置要靠 footer 或者从头扫才能拿到，漏掉一边合并，池子空了也拼不回去。

`stats()` 的语义要按下面来：

- `occupied`：当前所有已分配块占用的总字节数（块头加 payload），int。
- `fragmentation`：空闲内存碎片率，`1 - max_free / total_free`，float，在 0 到 1 之间；其中 `total_free = POOL_SIZE - occupied`。
- `max_free`：最大连续空闲块的总字节数（块头加 payload），int。

池子完全空闲时，`stats()` 必须等于 `(0, 0.0, 1048576)`。

`allocator.py` 还要能当命令行入口跑自测：

```bash
python3 allocator.py
```

自己跑一段随机 alloc/free 压力测试，通过就打印 `PASS` 并退出 0。

## 判分标准（`grade.py`）

```bash
python3 grade.py
```

`grade.py` 会 `import` 你的模块，跑一套固定种子的压力测试（10 万步，分配大小 64 到 512，同时存活不超过约 400KB），逐项校验：

- 返回地址不越界，`0 <= addr` 且 `addr + size <= POOL_SIZE`。
- 同时存活的两块分配区间互不重叠。
- 每块写进去的模式串在释放前保持原样，被改写就说明有越界写或者重叠分配。
- 全部释放后 `stats()` 回到 `(0, 0.0, 1048576)`，泄漏或者合并不完整都会露馅。
- 每隔一段校验一次 `stats()` 的返回值范围，模型函数抛异常也算失败。

另有命令行冒烟测试：`python3 allocator.py` 要能自跑并退出 0。

全部通过输出 `PASS`，退出码 0；任何一项不过输出 `FAIL`，退出码非 0。

## 约束

- 全程本地离线，只许用标准库。
- 不要用现成的分配器库，链表、分裂、合并、magic 校验全自己写。
- 真正的坑是内存布局：header 和 payload 的偏移、footer 存不存、合并时上一块怎么定位，这些写错压力测试一跑就现形，自己单测往往测不出来。
