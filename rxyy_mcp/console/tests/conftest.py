# -*- coding: utf-8 -*-
"""Syncthing 的冲突副本别参与收集。

两端同时改一个文件时，Syncthing 会在旁边落一份
`test_webgw.sync-conflict-20260821-085325-H5ATUWM.py`。它照样匹配 `test_*.py`，
但文件名里的点让 pytest 拼不出合法模块名，收集阶段直接 ImportError——一个副本
就能把整个目录 1000 多条测试拦在门外（08-24 实测：`Interrupted: 2 errors during
collection`，一条没跑）。

这些副本内容与正本并不相同（有的正本已经删了，副本是唯一留存），所以不能顺手删，
只能让 pytest 绕开。
"""

collect_ignore_glob = ["*.sync-conflict-*"]
