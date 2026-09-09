# -*- coding: utf-8 -*-
"""
indcalc_core.py — Maxwell 电感矩阵 -> 端口电感矩阵 推算核心
=============================================================
物理模型（磁链域 / 逆电感消去法）:

  原始 Maxwell 矩阵 L (N×N): λ = A·i , A = D·L·D (D 为各层极性 ±1 对角阵)

  1) 并联段 (segment): 段内各层电压相同 -> 磁链相同
     Γ_seg = B · A⁻¹ · Bᵀ      (B: K×N 段-层关联矩阵)
  2) 段间串联: 各段电流相同 (±), 磁链相加
     L_port = T · Γ_seg⁻¹ · Tᵀ  (T: M×K 端口-段方向矩阵, 元素 ±1)
  3) 未使用的层 = 开路 (i=0), 用 Schur 补从 A⁻¹ 中消去:
     Γ_eff = Γ[U,U] − Γ[U,O]·Γ[O,O]⁻¹·Γ[O,U]

段级混合拓扑 (段内模式可逐段独立指定):
  每个段可独立选择:
    * 段内并联 (parallel): 段内各层电压相同 -> 逆电感域合并
    * 段内串联 (series)  : 段内各层电流相同 -> 电感域合并
  端口级可选择段间连接:
    * 段间串联 (chain='series')   : 各段电流相同, 磁链相加
    * 段间并联 (chain='parallel') : 各段电压相同, 电流相加
  混合时用线性约束系统逐端口求解 (注入单位电流/磁链, 解层电流/磁链),
  纯模式仍走上面的解析公式 (数值精确)。

验证:
  两绕组并联(同向): L_eq = (L11·L22 − M²)/(L11 + L22 − 2M)   ✓
  两绕组串联(同向): L_eq = L11 + L22 + 2M                     ✓
  段1(A∥B) 串 段2(C):  L = 1/ΣΓ_AB + Σ_C(互感计入)          ✓
"""

import re
import numpy as np


# ----------------------------------------------------------------------
# 单位定义: (标签, 换算到 SI 基准的倍率)
#   电阻基准 = Ω, 电感基准 = H
# ----------------------------------------------------------------------
R_UNITS = [('Ω', 1.0), ('mΩ', 1e-3), ('µΩ', 1e-6)]
L_UNITS = [('H', 1.0), ('mH', 1e-3), ('µH', 1e-6), ('nH', 1e-9)]


# ----------------------------------------------------------------------
# Maxwell "电阻, 电感" 双值单元格格式 (如 '0.0024797, 108.4')
#   第一个数 = 电阻 (默认 Ω), 第二个数 = 电感 (默认 µH)
# ----------------------------------------------------------------------
_NUM = r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?:[uUmMnNkKhH]{0,2})'
_RL_CELL = re.compile(r'^(' + _NUM + r')\s*,\s*(' + _NUM + r')$')


def _rl_pair(field):
    """单元格若为 '电阻, 电感' 双值形式则返回 (r_str, l_str); 否则 None。"""
    m = _RL_CELL.match(field.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _tokenize_line(line):
    """把一行切分为单元格列表。

    每个单元格为:
      * 字符串       —— 标签或单值数值 (纯电感矩阵 / CSV)
      * (r, l) 元组  —— '电阻, 电感' 双值单元格

    两种分隔风格:
      A) 空白分隔 (制表符/多空格, Maxwell 导出): 逗号是单元格内部分隔 -> 双值元组
      B) 纯逗号/分号分隔 (CSV): 逗号是单元格边界 -> 单值字符串
    """
    fields = re.split(r'[;\t]+|\s{2,}', line)
    if len(fields) > 1:                       # 存在制表符/多空格 -> 按空白切单元格
        out = []
        for f in fields:
            f = f.strip()
            if not f:
                continue
            pair = _rl_pair(f)
            if pair is not None:
                out.append(pair)
            else:
                out.extend(x for x in (p.strip() for p in f.split(',')) if x)
        return out
    return [p.strip() for p in re.split(r'[,;]+', line) if p.strip()]


# ----------------------------------------------------------------------
# 矩阵解析
# ----------------------------------------------------------------------
def _tofloat(s, default_mult=1.0):
    """字符串 -> float。带单位后缀按后缀; 无后缀按 default_mult。"""
    s = s.strip()
    mult = default_mult
    sl = s.lower()
    if sl.endswith('uh'):
        s, mult = s[:-2], 1e-6
    elif sl.endswith('nh'):
        s, mult = s[:-2], 1e-9
    elif sl.endswith('mh'):
        s, mult = s[:-2], 1e-3
    elif sl.endswith('kh'):
        s, mult = s[:-2], 1e3
    elif sl.endswith('h'):
        s, mult = s[:-1], 1.0
    elif sl.endswith('u'):
        s, mult = s[:-1], 1e-6
    elif sl.endswith('n'):
        s, mult = s[:-1], 1e-9
    elif sl.endswith('m'):
        s, mult = s[:-1], 1e-3
    elif sl.endswith('k'):
        s, mult = s[:-1], 1e3
    return float(s) * mult


def _is_num_cell(cell):
    """单元格是否为数值 (字符串数值 或 (r,l) 元组)。"""
    if isinstance(cell, tuple):
        return True
    t = cell.strip().lower()
    for suf in ('uh', 'nh', 'mh', 'kh', 'u', 'n', 'm', 'k', 'h'):
        if t.endswith(suf) and len(t) > len(suf):
            t = t[:-len(suf)]
            break
    try:
        float(t)
        return True
    except ValueError:
        return False


def parse_matrix_rl(text, r_unit=1.0, l_unit=1e-6):
    """解析 '电阻, 电感' 双值矩阵为 (层名, R, L)。

    参数
    ----
    r_unit : 电阻无后缀时的默认单位倍率 (→Ω), 默认 1.0 (Ω)
    l_unit : 电感无后缀时的默认单位倍率 (→H), 默认 1e-6 (µH)

    返回
    ----
    names : 层名列表
    R     : N×N 电阻矩阵 (Ω); 若输入为纯电感单值矩阵则 R=None
    L     : N×N 电感矩阵 (H)

    单元格两种形式:
      '0.0024797, 108.4'  -> R=0.0024797*r_unit, L=108.4*l_unit
      '1.412u'            -> 仅电感 (纯电感矩阵), 电阻置 0
    """
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.replace('"', '').replace("'", '')
        parts = _tokenize_line(line)
        if parts:
            rows.append(parts)

    if len(rows) < 1:
        raise ValueError('矩阵内容为空')

    # 表头判定: 仅当首行"首尾单元格均非数值"视为表头
    header = None
    if rows and not _is_num_cell(rows[0][0]) and not _is_num_cell(rows[0][-1]) and len(rows[0]) > 1:
        header = rows[0][1:]
        rows = rows[1:]

    data_R, data_L, row_labels = [], [], []
    has_R = False
    for r in rows:
        if _is_num_cell(r[0]):
            cells = r
            row_labels.append(None)
        else:
            cells = r[1:]
            row_labels.append(r[0])
        rr, ll = [], []
        for c in cells:
            if isinstance(c, tuple):
                rr.append(_tofloat(c[0], r_unit))
                ll.append(_tofloat(c[1], l_unit))
                has_R = True
            else:
                rr.append(0.0)
                ll.append(_tofloat(c, l_unit))
        data_R.append(rr)
        data_L.append(ll)

    L = np.array(data_L, dtype=float)
    R = np.array(data_R, dtype=float)
    n = L.shape[0]
    if L.shape[0] != L.shape[1]:
        raise ValueError(f'矩阵不是方阵: {L.shape}')

    names = []
    for i in range(n):
        if row_labels and row_labels[i]:
            names.append(str(row_labels[i]))
        elif header and i < len(header):
            names.append(str(header[i]))
        else:
            names.append(f'W{i + 1}')

    if not has_R:
        R = None
    return names, R, L


def parse_matrix(text):
    """旧接口: 解析为 (层名, N×N 电感矩阵), 默认电感单位 μH。

    仅取电感值 (兼容双值矩阵, 忽略电阻)。"""
    names, R, L = parse_matrix_rl(text, r_unit=1.0, l_unit=1e-6)
    return names, L


# ----------------------------------------------------------------------
# 端口定义解析
# ----------------------------------------------------------------------
def parse_segment_entry(entry_text):
    """解析一个串联段的层列表: 'L1, -L2, L3' -> [('L1',+1), ('L2',-1), ('L3',+1)]

    层名前缀 '-' 表示该层反接 (同名端对调)。
    """
    out = []
    for tok in re.split(r'[,\s;]+', entry_text.strip()):
        if not tok:
            continue
        sign = +1
        if tok.startswith('-'):
            sign, tok = -1, tok[1:].strip()
        elif tok.startswith('+'):
            tok = tok[1:].strip()
        if not tok:
            raise ValueError('段内存在空层名')
        out.append((tok, sign))
    if not out:
        raise ValueError('串联段为空')
    return out


_SEG_PREFIX = re.compile(r'^(?:\[([PS])\]\s*|([PS])\s*[:：]\s*)', re.IGNORECASE)


def parse_segment_line(line, default_mode='parallel'):
    """解析一行段定义 -> (mode, [(层名, ±1), ...])

    mode 取 'parallel' (段内并联) 或 'series' (段内串联)。
    行首可加前缀 '[P]' / 'P:' / '[S]' / 'S:' 指定段内模式, 缺省用 default_mode。
    例: 'S: L1, -L2' -> ('series', [('L1',+1), ('L2',-1)])
    """
    line = line.strip()
    if not line:
        raise ValueError('段为空')
    mode = default_mode
    m = _SEG_PREFIX.match(line)
    if m:
        tag = (m.group(1) or m.group(2)).upper()
        mode = 'parallel' if tag == 'P' else 'series'
        line = line[m.end():].strip()
    if not line:
        raise ValueError('段内层列表为空')
    return mode, parse_segment_entry(line)


def parse_port_text(text, default_mode='parallel'):
    """解析端口定义文本 (每行一个段) -> [{'mode':…, 'layers':[(层,±1), …]}, …]

    每行 = 一个串联段, 行首可加 '[P]'/'S:' 等前缀指定段内模式, 缺省 default_mode。
    例:
        P: A1, A2     段1: A1∥A2 (段内并联)
        S: B1, B2     段2: B1串B2 (段内串联)
        C3            段3: 单层 C3 (缺省模式)
    """
    segments = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        mode, layers = parse_segment_line(line, default_mode)
        segments.append({'mode': mode, 'layers': layers})
    if not segments:
        raise ValueError('端口未定义任何串联段')
    return segments


# ----------------------------------------------------------------------
# 核心: 端口电感矩阵推算 (段内支路模型 + 段级混合拓扑)
#   段 (segment) 内部结构: 若干条并联支路 (branch)
#     * 支路内: 若干层串联 (电流相同, 磁链相加)
#     * 支路间: 并联 (电压相同, 电流相加)
#   特例: 每条支路单层 = 原"段内并联"; 单条支路多层 = 原"段内串联"
# ----------------------------------------------------------------------
def _build_branch_matrices(A, segs):
    """构造支路关联矩阵。

    参数
    ----
    A    : (N,N) 极性修正电感矩阵
    segs : [{'branches': [[(层索引,±1), …], …]}, …]

    返回
    ----
    E     : (N,B) 层-支路关联 (E[li, bi] = 1)
    FB    : (B,N) 支路磁链算子 (FB[bi,:] = Σ_{a∈支路bi} A[a,:])
    owner : (B,)  支路 -> 段编号
    B     : 总支路数
    """
    N = A.shape[0]
    owner = []
    for k, s in enumerate(segs):
        for _ in s['branches']:
            owner.append(k)
    B = len(owner)
    E = np.zeros((N, B))
    FB = np.zeros((B, N))
    bi = 0
    for k, s in enumerate(segs):
        for b in s['branches']:
            for (li, _) in b:
                E[li, bi] = 1.0
                FB[bi, :] += A[li, :]
            bi += 1
    return E, FB, owner, B


def _solve_series_chain(A, segs, open_layers, seg_current):
    """段间串联 + 段内支路模型: 给定各段电流(含方向), 解层电流 i。

    段间串联 -> 各段电流已知 (= 端口电流投影, 常数);
    段内: 支路内层串联 (等电流), 支路间并联 (电流和 = 段电流, 支路磁链相等)。
    未知量取支路电流 ib (B 维), 层电流 i = E·ib; 未用层不在任何支路, 电流自动为 0。

    方程 (恰为 B 个):
      * 每段 1 个: Σ_{b∈k} ib_b = seg_current[k]          (支路电流和 = 段电流)
      * 每段 (支路数−1) 个: 支路磁链相等 (M2·ib 同段内相等)
    返回 (N,) 层电流。
    """
    E, FB, owner, B = _build_branch_matrices(A, segs)
    M2 = FB @ E                      # (B,B): 支路磁链 = M2 @ ib
    eq, rhs = [], []
    bi = 0
    for k, s in enumerate(segs):
        nb = len(s['branches'])
        row = np.zeros(B)
        row[bi:bi + nb] = 1.0        # 支路电流和 = 段电流
        eq.append(row); rhs.append(seg_current[k])
        for j in range(1, nb):       # 支路磁链相等 (以第 0 条支路为基准)
            eq.append(M2[bi] - M2[bi + j]); rhs.append(0.0)
        bi += nb
    ib = np.linalg.lstsq(np.array(eq), np.array(rhs), rcond=None)[0]
    return E @ ib                    # 层电流


def _solve_parallel_chain(G, segs, open_layers, seg_flux):
    """段间并联 + 段内支路模型: 给定各段磁链(含方向), 解层磁链 λ。

    段间并联 -> 各段磁链已知 (= 端口磁链投影, 常数);
    段内: 支路间并联 (支路磁链 = 段磁链), 支路内层串联 (等电流)。
    未知量取层磁链 λ (N 维)。

    方程 (恰为 N 个):
      * 每条支路 1 个: Σ_{a∈b} λ_a = seg_flux[k]           (支路磁链 = 段磁链)
      * 每条支路 (层数−1) 个: 支路内 (G·λ)_a 相等 (层电流相同)
      * 每个开路层 1 个: (G·λ)_a = 0 (层电流 0)
    返回 (N,) 层磁链。
    """
    N = G.shape[0]
    eq, rhs = [], []
    for k, s in enumerate(segs):
        fk = seg_flux[k]
        for b in s['branches']:
            ls = [li for li, _ in b]
            row = np.zeros(N)
            for li in ls:
                row[li] = 1.0       # 支路磁链 Σλ = 段磁链
            eq.append(row); rhs.append(fk)
            for li in ls[1:]:
                eq.append(G[ls[0]] - G[li]); rhs.append(0.0)
    for a in open_layers:
        eq.append(G[a]); rhs.append(0.0)
    return np.linalg.lstsq(np.array(eq), np.array(rhs), rcond=None)[0]


def compute_port_matrix(L, layer_names, ports, keep_unused_open=True):
    """
    参数
    ----
    L            : (N,N) 原始电感矩阵 (Henry)
    layer_names  : 长度 N 的层名列表
    ports        : [{'name': str, 'chain_mode': 'series'|'parallel',
                     'segments': [ {'branches': [ [(层名,±1), …], … ]}, … ]}, …]
                   - chain_mode: 段间连接方式 ('series' 段间串联 / 'parallel' 段间并联)
                   - 段 (segment) 内部: 若干条并联支路 (branch)
                       * 支路内: 若干层串联 (电流相同, 磁链相加)
                       * 支路间: 并联 (电压相同, 电流相加)
                     (向后兼容: 段可为 {'mode','layers'} (mode 'parallel'=层全并联/单层支路,
                      'series'=层全串联/单支路) 或纯列表 [(层,±1),…])
                   - 层名前缀 '-' 表示反接 (同名端对调)
    keep_unused_open : True 时未引用层按开路(i=0)消去

    返回
    ----
    L_port : (M,M) 端口电感矩阵 (Henry)
    info   : 诊断信息 dict (各段成员、等效电感等)

    两种段间连接方式在数学上对偶:
      段间串联: 各段电流相同, 磁链相加 -> 注入端口电流, 求端口磁链
      段间并联: 各段电压相同, 电流相加 -> 注入端口磁链, 求端口电流, 再取逆
    纯模式 (所有段每条支路单层, 或所有段单支路) 走解析公式; 其余走通用约束求解器。
    """
    L = np.asarray(L, dtype=float)
    N = L.shape[0]
    if L.shape[0] != L.shape[1]:
        raise ValueError('电感矩阵非方阵')
    idx = {n: i for i, n in enumerate(layer_names)}
    if len(idx) != N:
        raise ValueError(f'层名数量不匹配: {len(idx)} 个唯一名 vs 矩阵 {N} 阶')

    # ---- 归一化端口: 段 -> {'branches': [[(层名,±1),…],…]} ----
    def _norm_segment(s, seg_default):
        """段输入 -> 支路列表 [[(层名,±1),…], …] (支路内串联, 支路间并联)。"""
        if isinstance(s, dict):
            if 'branches' in s:                     # 新格式
                branches = [list(b) for b in s['branches']]
            else:                                   # 旧格式 mode+layers
                mode = s.get('mode', seg_default)
                layers = list(s.get('layers', []))
                if mode == 'parallel':
                    branches = [[l] for l in layers]        # 层全并联 = 单层支路并联
                elif mode == 'series':
                    branches = [list(layers)]               # 层全串联 = 单支路
                else:
                    raise ValueError(f'未知段内模式: {mode}')
        else:                                       # 最旧格式: 纯列表
            layers = list(s)
            if seg_default == 'parallel':
                branches = [[l] for l in layers]
            else:
                branches = [list(layers)]
        branches = [b for b in branches if b]
        if not branches:
            raise ValueError('段内没有层')
        return branches

    chain_modes = set()
    norm_ports = []
    for p in ports:
        raw_chain = p.get('chain_mode', p.get('mode', 'parallel_series'))
        if raw_chain in ('parallel_series', 'series'):
            chain_mode, seg_default = 'series', 'parallel'
        elif raw_chain in ('series_parallel', 'parallel'):
            chain_mode, seg_default = 'parallel', 'series'
        else:
            raise ValueError(f'未知连接方式: {raw_chain}')
        chain_modes.add(chain_mode)
        norm_ports.append({'name': p.get('name', ''), 'chain_mode': chain_mode,
                           'segments': [_norm_segment(s, seg_default)
                                        for s in p['segments']]})
    if len(chain_modes) > 1:
        raise ValueError('当前版本要求所有端口段间连接方式一致 (串联 或 并联)')
    chain_mode = chain_modes.pop()

    # ---- 展开段: 校验层名 & 唯一性 ----
    seg_list = []    # [{'branches': [[(li,±1), …], …]}]
    seg_port = []    # 段 -> 端口编号
    used = np.zeros(N, dtype=bool)
    for pi, port in enumerate(norm_ports):
        for branches in port['segments']:
            sb = []
            for b in branches:
                mb = []
                for (lname, sgn) in b:
                    if lname not in idx:
                        raise ValueError(f'层名 "{lname}" 不在电感矩阵中 (可用: {layer_names})')
                    li = idx[lname]
                    if used[li]:
                        raise ValueError(f'层 "{lname}" 被重复使用于多个段/支路 (每层只能出现一次)')
                    used[li] = True
                    mb.append((li, float(sgn)))
                sb.append(mb)
            seg_list.append({'branches': sb})
            seg_port.append(pi)

    K = len(seg_list)
    M = len(norm_ports)
    if K == 0:
        raise ValueError('没有定义任何段')

    # ---- 极性阵 D, 极性修正矩阵 A, 逆电感 G, 端口-段方向阵 T ----
    D = np.eye(N)
    for s in seg_list:
        for b in s['branches']:
            for (li, sgn) in b:
                D[li, li] = sgn
    A = D @ L @ D
    G = np.linalg.inv(A)
    T = np.zeros((M, K))
    for k in range(K):
        T[seg_port[k], k] = 1.0   # 段方向恒 +1, 相对极性由层 '-' 表达

    # 纯模式判断 (走解析公式, 数值与原实现一致):
    #   纯层并联: 段间串联 + 所有段每条支路单层 (支路间并联 = 层直接并联)
    #   纯层串联: 段间并联 + 所有段单条支路 (支路内串联 = 层直接串联)
    pure_parallel = (chain_mode == 'series'
                     and all(len(br) == 1 for s in seg_list for br in s['branches']))
    pure_series = (chain_mode == 'parallel'
                   and all(len(s['branches']) == 1 for s in seg_list))

    # ---- 纯模式: 解析公式 (与原实现数值一致) ----
    if pure_parallel or pure_series:
        if keep_unused_open and not used.all():
            U, O = used, ~used
            G_eff = (G[np.ix_(U, U)]
                     - G[np.ix_(U, O)] @ np.linalg.solve(G[np.ix_(O, O)], G[np.ix_(O, U)]))
        else:
            G_eff = G
        B_u = np.zeros((K, N))
        for k, s in enumerate(seg_list):
            for b in s['branches']:
                for (li, _) in b:
                    B_u[k, li] = 1.0
        if keep_unused_open:
            B_u = B_u[:, used]
        if pure_parallel:
            # 段内(层)并联 + 段间串联: L = T·(B·G_eff·Bᵀ)⁻¹·Tᵀ
            G_seg = B_u @ G_eff @ B_u.T
            G_seg = 0.5 * (G_seg + G_seg.T)
            try:
                L_seg = np.linalg.inv(G_seg)
            except np.linalg.LinAlgError:
                L_seg = np.linalg.pinv(G_seg)
            L_port = T @ L_seg @ T.T
        else:
            # 段内(层)串联 + 段间并联: L = (T·(B·A_eff·Bᵀ)⁻¹·Tᵀ)⁻¹
            A_eff = np.linalg.inv(G_eff)
            L_seg = B_u @ A_eff @ B_u.T
            L_seg = 0.5 * (L_seg + L_seg.T)
            try:
                L_seg_inv = np.linalg.inv(L_seg)
            except np.linalg.LinAlgError:
                L_seg_inv = np.linalg.pinv(L_seg)
            try:
                L_port = np.linalg.inv(T @ L_seg_inv @ T.T)
            except np.linalg.LinAlgError:
                L_port = np.linalg.pinv(T @ L_seg_inv @ T.T)
    else:
        # ---- 通用模式: 约束求解器 (逐端口注入) ----
        open_layers = [a for a in range(N) if not used[a]] if keep_unused_open else []
        if chain_mode == 'series':
            L_port = np.zeros((M, M))
            for q in range(M):
                i = _solve_series_chain(A, seg_list, open_layers, T[q, :])
                lam = A @ i
                for p in range(M):
                    acc = 0.0
                    for k in range(K):
                        if seg_port[k] != p:
                            continue
                        # 段内支路间并联 -> 段磁链 = 任一支路磁链 (层磁链和)
                        b0 = seg_list[k]['branches'][0]
                        acc += sum(lam[li] for li, _ in b0)
                    L_port[p, q] = acc
        else:  # chain_mode == 'parallel'
            Gam = np.zeros((M, M))
            for q in range(M):
                lam = _solve_parallel_chain(G, seg_list, open_layers, T[q, :])
                cur = G @ lam
                for p in range(M):
                    acc = 0.0
                    for k in range(K):
                        if seg_port[k] != p:
                            continue
                        # 段内支路间并联 -> 段电流 = 各支路电流之和 (支路内任一层电流)
                        for b in seg_list[k]['branches']:
                            acc += cur[b[0][0]]
                    Gam[p, q] = acc
            try:
                L_port = np.linalg.inv(Gam)
            except np.linalg.LinAlgError:
                L_port = np.linalg.pinv(Gam)
    L_port = 0.5 * (L_port + L_port.T)

    # ---- 诊断信息 ----
    seg_names = []
    for k, s in enumerate(seg_list):
        parts = []
        for b in s['branches']:
            parts.append('(' + '串'.join(('−' if sg < 0 else '') + layer_names[li]
                                         for (li, sg) in b) + ')')
        desc = '∥'.join(parts)
        seg_names.append(f"[{norm_ports[seg_port[k]]['name']}#{k + 1}] {desc}")
    # 段等效电感: 段间串联 -> 单位链电流下各段磁链; 段间并联 -> 段磁链 1 时的电流倒数
    if pure_parallel or pure_series:
        seg_L = np.diag(L_seg) if K > 0 else []
    else:
        seg_L = []
        open_layers = [a for a in range(N) if not used[a]] if keep_unused_open else []
        if chain_mode == 'series':
            i = _solve_series_chain(A, seg_list, open_layers, np.ones(K))
            lam = A @ i
            for k, s in enumerate(seg_list):
                b0 = s['branches'][0]
                seg_L.append(sum(lam[li] for li, _ in b0))
        else:
            lam = _solve_parallel_chain(G, seg_list, open_layers, np.ones(K))
            cur = G @ lam
            for k, s in enumerate(seg_list):
                ik = sum(cur[b[0][0]] for b in s['branches'])
                seg_L.append(1.0 / ik if abs(ik) > 1e-300 else float('inf'))
    info = {
        'seg_names': seg_names,
        'seg_L': seg_L,
        'K': K, 'M': M,
        'chain_mode': chain_mode,
        'mode': 'series_parallel' if chain_mode == 'parallel' else 'parallel_series',
        'unused_layers': [layer_names[i] for i in range(N) if not used[i]],
    }
    return L_port, info


# ----------------------------------------------------------------------
# 变压器 T 型等效参数 (两端口: 原边 / 副边)
# ----------------------------------------------------------------------
def compute_t_equiv(L, port_names, prim, sec):
    """由端口电感矩阵推导原边-副边两端口变压器 T 型等效参数。

    输入
    ----
    L         : (M,M) 端口电感矩阵 (Henry)
    port_names: 端口名列表
    prim, sec : 原边端口名、副边端口名

    物理模型 (2×2 子矩阵)
    ----
        L = [[Lpp, M],
             [M,  Lss]]
    其中 Lpp=原边自感, Lss=副边自感, M=互感。

    T 型等效 (理想变压器 1:n + 励磁 Lm + 两侧漏感), 匝比 n = Np/Ns (原边/副边):
        Lpp = Lkp + Lm
        M   = Lm / n
        Lss = Lks + Lm / n²
    匝比取对称约定 n = √(Lpp/Lss) (> 0, 恒正), 使得
        Lm  = k·Lpp,  Lkp = Lpp·(1−k),  Lks = Lss·(1−k),  k = |M|/√(Lpp·Lss)
    互感 M<0 (反极性) 时按 |M| 计算 (匝比/漏感/励磁均取正, 负号仅表示同名端相反)。

    第二步: 副边漏感全部折算到原边 (副边支路无漏感, 端口矩阵严格不变):
        Lkp1 = Lpp − M²/Lss  (= Lpp·(1−k²))
        Lm1  = M²/Lss        (= k²·Lpp)
        n1   = |M|/Lss       (= k·n, 恒正)

    返回
    ----
    dict: Lpp, Lss, M, k, n, Lkp, Lks, Lm, n1, Lkp1, Lm1 (电感单位 H, 匝比无量纲)
    """
    L = np.asarray(L, dtype=float)
    try:
        i, j = port_names.index(prim), port_names.index(sec)
    except ValueError:
        raise ValueError(f'端口名不在矩阵中: {prim} / {sec}')
    Lpp, Lss = L[i, i], L[j, j]
    M = L[i, j]
    if Lpp <= 0 or Lss <= 0:
        raise ValueError(f'端口自感必须为正: Lpp={Lpp:g}, Lss={Lss:g}')
    if abs(M) < 1e-300:
        raise ValueError('互感 M ≈ 0, 两端口无耦合, 无法构成变压器等效')
    k = abs(M) / np.sqrt(Lpp * Lss)
    k = min(k, 1.0 - 1e-15)               # 数值保护
    Am = abs(M)                           # 负互感仅表极性, 参数计算用幅值
    n = np.sqrt(Lpp / Lss)                # 对称匝比 Np/Ns, 恒正
    Lm = n * Am                           # > 0
    Lkp = Lpp - Lm
    Lks = Lss - Am / n                    # = Lss - Lm/n² ≥ 0
    # ---- 副边漏感全部折算到原边 (副边无漏感, 端口矩阵严格一致) ----
    Lkp1 = Lpp - M * M / Lss              # = Lpp(1-k²)
    Lm1 = M * M / Lss                     # = k²·Lpp
    n1 = Am / Lss                         # = k·n, 恒正
    return {
        'Lpp': Lpp, 'Lss': Lss, 'M': M, 'k': k,
        'n': n, 'Lkp': Lkp, 'Lks': Lks, 'Lm': Lm,
        'n1': n1, 'Lkp1': Lkp1, 'Lm1': Lm1,
    }


# ----------------------------------------------------------------------
# 多绕组 (≥3 端口) 变压器等效 & 副边并联均流分析
# ----------------------------------------------------------------------
def _sub_matrix(L, port_names, ports):
    """从端口矩阵提取指定端口的子矩阵与索引。"""
    idx = [port_names.index(p) for p in ports]
    return L[np.ix_(idx, idx)]


def compute_multi_t_equiv(L, port_names, prim, secs):
    """多绕组变压器等效参数 (N 端口, N≥2)。

    输入
    ----
    L         : (M,M) 端口电感矩阵 (Henry)
    port_names: 端口名列表
    prim      : 原边端口名
    secs      : 副边端口名列表 (1 个或多个)

    物理模型 (对每个副边绕组, 相对原边):
        n_i = √(Lpp/Lss_i)         匝比 Np/Ns (>0)
        k_i = |Mpi|/√(Lpp·Lss_i)   该副边对原边耦合系数
        Lks_i = Lss_i − |Mpi|/n_i  副边自身漏感 (副边侧)
        折算到原边漏感 = Lpp·(1−k_i)  (副边漏感 × n_i²)
    副边绕组之间:
        k_ij = Mij/√(Lss_i·Lss_j)  副边间耦合系数 (与互感同号)

    返回
    ----
    dict: prim, secs, Lpp, n[], k[], Lks[], Lks_ref[], k_sec (副边间耦合系数表)
    """
    L = np.asarray(L, dtype=float)
    i_p = port_names.index(prim)
    Lpp = L[i_p, i_p]
    if Lpp <= 0:
        raise ValueError('原边自感必须为正')
    out = {'prim': prim, 'secs': list(secs), 'Lpp': Lpp,
           'n': {}, 'k': {}, 'Lks': {}, 'Lks_ref': {}}
    sec_idx = {}
    for s in secs:
        i_s = port_names.index(s)
        sec_idx[s] = i_s
        Lss = L[i_s, i_s]
        Mps = L[i_p, i_s]
        if Lss <= 0:
            raise ValueError(f'副边 {s} 自感必须为正')
        if abs(Mps) < 1e-300:
            raise ValueError(f'原边与副边 {s} 互感 ≈0, 无耦合')
        n = np.sqrt(Lpp / Lss)                # Np/Ns
        Am = abs(Mps)
        k = Am / np.sqrt(Lpp * Lss)
        k = min(k, 1.0 - 1e-15)
        Lks = Lss - Am / n                    # 副边自身漏感
        out['n'][s] = n
        out['k'][s] = k
        out['Lks'][s] = Lks
        out['Lks_ref'][s] = Lpp * (1.0 - k)   # 折算到原边
    # 副边间耦合系数表
    ksec = {}
    for a in secs:
        for b in secs:
            if a == b:
                continue
            ia, ib = sec_idx[a], sec_idx[b]
            ksec[(a, b)] = L[ia, ib] / np.sqrt(L[ia, ia] * L[ib, ib])
    out['k_sec'] = ksec
    return out


def eval_secondary_parallel(L, port_names, prim, sec1, sec2, ip_scale=1.0,
                            load_r=0.1, freq=100e3):
    """两个副边绕组并联 (带载) 时的均流分析 (频域稳态)。

    物理模型: 三端口 [P, S1, S2] 电感矩阵
        L = [[Lpp, Mp1, Mp2],
             [Mp1, Ls1, M12],
             [Mp2, M12, Ls2]]
    副边并联共同驱动负载 R: v_s1 = v_s2 = v_s = R·(I1+I2)。
    令 W = R/(jω) (负载折算为等效电感), 方程组:
        (Ls1+W)·I1 + (M12+W)·I2 = −Mp1·Ip
        (M12+W)·I1 + (Ls2+W)·I2 = −Mp2·Ip
    load_r=0 即短路 (副边磁链=0); load_r→∞ 即开路。

    返回
    ----
    dict: ip, v_p, i1, i2, v_s, share1, share2, imbalance
        v_p = jω·(Lpp·Ip + Mp1·I1 + Mp2·I2)  原边端口电压
        v_s                                      副边并联端口电压
        share1 = |I1|/(|I1|+|I2|)  (理想 0.5 = 完全均流)
        imbalance = 2·|I1−I2|/(|I1|+|I2|)  (0 = 完全均流)
    """
    ip = complex(ip_scale, 0)
    Ms = _sub_matrix(L, port_names, [prim, sec1, sec2])
    Lpp = Ms[0, 0]
    Mp1, Mp2 = Ms[0, 1], Ms[0, 2]
    Ls1, Ls2 = Ms[1, 1], Ms[2, 2]
    M12 = Ms[1, 2]
    W = load_r / (1j * 2 * np.pi * freq)     # 负载折算 (H)
    A = np.array([[Ls1 + W, M12 + W],
                  [M12 + W, Ls2 + W]], dtype=complex)
    b = np.array([-Mp1 * ip, -Mp2 * ip], dtype=complex)
    i = np.linalg.solve(A, b)
    i1, i2 = i[0], i[1]
    omega = 2 * np.pi * freq
    v_p = 1j * omega * (Lpp * ip + Mp1 * i1 + Mp2 * i2)
    v_s = -1j * omega * (Mp1 * ip + Ls1 * i1 + M12 * i2)
    a1, a2 = abs(i1), abs(i2)
    if a1 + a2 < 1e-300:
        raise ValueError('副边电流 ≈0')
    share1 = a1 / (a1 + a2)
    share2 = a2 / (a1 + a2)
    imbalance = 2.0 * abs(a1 - a2) / (a1 + a2)
    return {'ip': ip, 'v_p': v_p, 'i1': i1, 'i2': i2, 'v_s': v_s,
            'share1': share1, 'share2': share2, 'imbalance': imbalance}


def scan_sec_coupling(L, port_names, prim, sec1, sec2, n_points=41,
                      load_r=0.1, freq=100e3):
    """扫描副边间耦合系数 k12 ∈ [0, 可行上限] 对均流的影响 (带载模型)。

    方法: 固定 Lpp/Ls1/Ls2/Mp1/Mp2 不变, 令 M12 = k12·√(Ls1·Ls2),
    在每个 k12 下解副边并联带载方程, 得均流比 share1 与不平衡度。
    k12 上限受三端口矩阵正定性约束 (det > 0), 超出部分截断。

    返回
    ----
    dict: k12[], share1[], imbalance[], k12_max (可行上限)
    """
    Ms = _sub_matrix(L, port_names, [prim, sec1, sec2])
    Lpp, Ls1, Ls2 = Ms[0, 0], Ms[1, 1], Ms[2, 2]
    Mp1, Mp2 = Ms[0, 1], Ms[0, 2]
    # 正定性约束: det(L3) > 0, 二分查找 k12 可行上限
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        M12 = mid * np.sqrt(Ls1 * Ls2)
        D = np.array([[Lpp, Mp1, Mp2],
                      [Mp1, Ls1, M12],
                      [Mp2, M12, Ls2]])
        if np.linalg.det(D) > 1e-18 * Lpp * Ls1 * Ls2:
            lo = mid
        else:
            hi = mid
    k_max = max(0.0, lo)
    ks = np.linspace(0.0, k_max, n_points)
    shares, imbs = [], []
    W = load_r / (1j * 2 * np.pi * freq)
    for k12 in ks:
        M12 = k12 * np.sqrt(Ls1 * Ls2)
        A = np.array([[Ls1 + W, M12 + W],
                      [M12 + W, Ls2 + W]], dtype=complex)
        b = np.array([-Mp1, -Mp2], dtype=complex)
        i = np.linalg.solve(A, b)
        a1, a2 = abs(i[0]), abs(i[1])
        if a1 + a2 < 1e-300:
            shares.append(0.5); imbs.append(0.0)
            continue
        shares.append(a1 / (a1 + a2))
        imbs.append(2.0 * abs(a1 - a2) / (a1 + a2))
    return {'k12': ks.tolist(), 'share1': shares, 'imbalance': imbs,
            'k12_max': k_max}


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------
def _selftest():
    # 两耦合绕组: L11=2u, L22=3u, M=1u
    L = np.array([[2e-6, 1e-6],
                  [1e-6, 3e-6]])
    names = ['A', 'B']

    # 并联 (同向): (2*3-1)/(2+3-2)=5/3 uH
    ports = [{'name': 'P', 'segments': [[('A', 1), ('B', 1)]]}]
    Lp, _ = compute_port_matrix(L, names, ports)
    exp = 5 / 3 * 1e-6
    assert abs(Lp[0, 0] - exp) < 1e-15, f'并联失败: {Lp[0,0]} vs {exp}'

    # 串联 (同向): 2+3+2=7uH
    ports = [{'name': 'P', 'segments': [[('A', 1)], [('B', 1)]]}]
    Lp, _ = compute_port_matrix(L, names, ports)
    assert abs(Lp[0, 0] - 7e-6) < 1e-15, f'串联失败: {Lp[0,0]}'

    # 串联 (反向): 2+3-2=3uH
    ports = [{'name': 'P', 'segments': [[('A', 1)], [('B', -1)]]}]
    Lp, _ = compute_port_matrix(L, names, ports)
    assert abs(Lp[0, 0] - 3e-6) < 1e-15, f'反向串联失败: {Lp[0,0]}'

    # 先串联后并联 (series_parallel): 四个层两两串联成两支路, 两支路并联
    # 层: A(2u) B(3u) C(2u) D(3u); 互感 A-B=M(1u), C-D=M(1u), 跨支路互感=0
    L4 = np.array([[2e-6, 1e-6, 0, 0],
                   [1e-6, 3e-6, 0, 0],
                   [0, 0, 2e-6, 1e-6],
                   [0, 0, 1e-6, 3e-6]])
    n4 = ['A', 'B', 'C', 'D']
    # 支路1=A串B: 2+3+2=7u; 支路2=C串D: 7u; 两支路(无互感)并联: 7/2=3.5u
    ports = [{'name': 'P', 'mode': 'series_parallel',
              'segments': [[('A', 1), ('B', 1)], [('C', 1), ('D', 1)]]}]
    Lp, info = compute_port_matrix(L4, n4, ports)
    assert abs(Lp[0, 0] - 3.5e-6) < 1e-12, f'先串后并失败: {Lp[0,0]}'

    # 单层支路并联退化: 与普通并联一致 (2*3-1)/(2+3-2)=5/3 uH
    ports = [{'name': 'P', 'mode': 'series_parallel',
              'segments': [[('A', 1)], [('B', 1)]]}]
    Lp, _ = compute_port_matrix(L, names, ports)
    assert abs(Lp[0, 0] - 5 / 3 * 1e-6) < 1e-15, f'单层支路并联退化失败: {Lp[0,0]}'

    # 混合模式应报错
    try:
        compute_port_matrix(L, names,
                            [{'name': 'P1', 'segments': [[('A', 1)]]},
                             {'name': 'P2', 'mode': 'series_parallel',
                              'segments': [[('B', 1)]]}])
        raise AssertionError('混合模式未报错')
    except ValueError:
        pass

    # 双端口: P1=(A 串 B), P2 悬空 -> 开路; 再测 P1=A, P2=B 全矩阵 [[2,1],[1,3]]
    ports = [{'name': 'P1', 'segments': [[('A', 1)]]},
             {'name': 'P2', 'segments': [[('B', 1)]]}]
    Lp, _ = compute_port_matrix(L, names, ports)
    assert np.allclose(Lp, L * 1.0, atol=1e-18)

    # 三层: C 只与 A 耦合, C 开路 vs C 并入
    L3 = np.array([[2e-6, 1e-6, 0.5e-6],
                   [1e-6, 3e-6, 0.0],
                   [0.5e-6, 0.0, 4e-6]])
    n3 = ['A', 'B', 'C']
    # C 开路: 端口 A-B 矩阵仍为 [[2,1],[1,3]]
    ports = [{'name': 'P1', 'segments': [[('A', 1)]]},
             {'name': 'P2', 'segments': [[('B', 1)]]}]
    Lp, _ = compute_port_matrix(L3, n3, ports)
    assert np.allclose(Lp, [[2e-6, 1e-6], [1e-6, 3e-6]]), f'开路消去失败: {Lp}'

    # ---- 段级混合拓扑 ----
    # (1) 段1 = A∥B (段内并联), 段2 = C (单层), 段间串联
    #     注入 I=1: i_a=2/3, i_b=1/3, i_c=1
    #     λ_ab = 2·(2/3) + 1·(1/3) + 0.5·1 = 13/6
    #     λ_c  = 0.5·(2/3) + 0.5·(1/3) + 4·1 = 9/2
    #     L = 13/6 + 9/2 = 40/6 = 20/3 µH
    L5 = np.array([[2e-6, 1e-6, 0.5e-6],
                   [1e-6, 3e-6, 0.5e-6],
                   [0.5e-6, 0.5e-6, 4e-6]])
    n5 = ['A', 'B', 'C']
    ports = [{'name': 'P', 'chain_mode': 'series',
              'segments': [{'mode': 'parallel', 'layers': [('A', 1), ('B', 1)]},
                           {'mode': 'series', 'layers': [('C', 1)]}]}]
    Lp, info = compute_port_matrix(L5, n5, ports)
    assert abs(Lp[0, 0] - 20 / 3 * 1e-6) < 1e-12, f'混合(并+串)失败: {Lp[0,0]}'

    # (2) 段1 = A 串 B (段内串联), 段2 = C (单层), 段间串联
    #     注入 I=1: i_a=i_b=1, i_c=1
    #     λ_a = 2+1+0.5 = 3.5; λ_b = 1+3+0.2 = 4.2 -> λ_ab = 7.7
    #     λ_c = 0.5+0.2+4 = 4.7; L = 12.4 µH
    L6 = np.array([[2e-6, 1e-6, 0.5e-6],
                   [1e-6, 3e-6, 0.2e-6],
                   [0.5e-6, 0.2e-6, 4e-6]])
    n6 = ['A', 'B', 'C']
    ports = [{'name': 'P', 'chain_mode': 'series',
              'segments': [{'mode': 'series', 'layers': [('A', 1), ('B', 1)]},
                           {'mode': 'parallel', 'layers': [('C', 1)]}]}]
    Lp, _ = compute_port_matrix(L6, n6, ports)
    assert abs(Lp[0, 0] - 12.4e-6) < 1e-12, f'混合(串+并)失败: {Lp[0,0]}'

    # (3) 段间并联 + 混合段模式: 段1 = A 串 B, 段2 = C∥D, 段间并联
    #     段1: 2+3+2·1 = 7µ; 段2: (5·6−4)/(5+6−4) = 26/7 µ; 无跨耦合
    #     L = 7 ∥ (26/7) = 182/75 µH
    L4m = np.array([[2e-6, 1e-6, 0, 0],
                    [1e-6, 3e-6, 0, 0],
                    [0, 0, 5e-6, 2e-6],
                    [0, 0, 2e-6, 6e-6]])
    n4m = ['A', 'B', 'C', 'D']
    ports = [{'name': 'P', 'chain_mode': 'parallel',
              'segments': [{'mode': 'series', 'layers': [('A', 1), ('B', 1)]},
                           {'mode': 'parallel', 'layers': [('C', 1), ('D', 1)]}]}]
    Lp, _ = compute_port_matrix(L4m, n4m, ports)
    assert abs(Lp[0, 0] - 182 / 75 * 1e-6) < 1e-12, f'混合(段间并联)失败: {Lp[0,0]}'

    # (4) 文本解析 + 混合模式: 'S:' 前缀
    segs = parse_port_text('P: A, B\nS: C, D')
    assert segs == [{'mode': 'parallel', 'layers': [('A', 1), ('B', 1)]},
                    {'mode': 'series', 'layers': [('C', 1), ('D', 1)]}], f'前缀解析失败: {segs}'

    # ---- 段内并联支路模型 ----
    # (5) 段 = 2 条并联支路 (支路内串联): (A串B) ∥ (C串D), 段间串联(单段)
    #     支路1 = A串B: 2+3+2·1 = 7µ; 支路2 = C串D: 5+6+2·2 = 15µ; 无跨耦合
    #     L = 7 ∥ 15 = 105/22 µH
    L7 = np.array([[2e-6, 1e-6, 0, 0],
                   [1e-6, 3e-6, 0, 0],
                   [0, 0, 5e-6, 2e-6],
                   [0, 0, 2e-6, 6e-6]])
    n7 = ['A', 'B', 'C', 'D']
    ports = [{'name': 'P', 'chain_mode': 'series',
              'segments': [{'branches': [[('A', 1), ('B', 1)], [('C', 1), ('D', 1)]]}]}]
    Lp, info = compute_port_matrix(L7, n7, ports)
    assert abs(Lp[0, 0] - 105 / 22 * 1e-6) < 1e-12, f'并联支路(串内并)失败: {Lp[0,0]}'

    # (6) 段 = (A串B) ∥ C (支路1 串联, 支路2 单层), 段间串联; 跨支路有耦合
    #     注入 I=1: i_A=i_B=ib1, i_C=ib2; ib1+ib2=1
    #     λ1 = λ_A+λ_B = 7ib1+ib2; λ2 = λ_C = ib1+4ib2; 等磁链 -> ib2=2ib1
    #     ib1=1/3, ib2=2/3; L = λ1 = 3 µH
    ports = [{'name': 'P', 'chain_mode': 'series',
              'segments': [{'branches': [[('A', 1), ('B', 1)], [('C', 1)]]}]}]
    Lp, _ = compute_port_matrix(L5, n5, ports)
    assert abs(Lp[0, 0] - 3e-6) < 1e-12, f'并联支路(串∥单层)失败: {Lp[0,0]}'

    # (7) 旧格式 v2 (mode+layers) 与 v3 (branches) 一致性
    for (v2, v3) in (
        ([{'name': 'P', 'chain_mode': 'series',
           'segments': [{'mode': 'parallel', 'layers': [('A', 1), ('B', 1)]}]}],
         [{'name': 'P', 'chain_mode': 'series',
           'segments': [{'branches': [[('A', 1)], [('B', 1)]]}]}]),
        ([{'name': 'P', 'chain_mode': 'series',
           'segments': [{'mode': 'series', 'layers': [('A', 1), ('B', 1)]}]}],
         [{'name': 'P', 'chain_mode': 'series',
           'segments': [{'branches': [[('A', 1), ('B', 1)]]}]}]),
    ):
        La, _ = compute_port_matrix(L, names, v2)
        Lb, _ = compute_port_matrix(L, names, v3)
        assert abs(La[0, 0] - Lb[0, 0]) < 1e-18, f'v2/v3 不一致: {La[0,0]} vs {Lb[0,0]}'

    # (8) 段间并联 + 支路: 段1 = (A串B)∥C, 段2 = D(单层), 段间并联
    #     注入 λ_port=1: λ_C=λ_D=1; λ_A+λ_B=1 且 i_A=i_B -> λ_A=3/7, λ_B=4/7
    #     i_AB = 1/7, i_C = (6λ_C−2λ_D)/26 = 2/13, i_D = (−2λ_C+5λ_D)/26 = 3/26
    #     I = 27/91 + 3/26 = 975/2366;  L = 2366/975 µH (含 C-D 跨段耦合)
    ports = [{'name': 'P', 'chain_mode': 'parallel',
              'segments': [{'branches': [[('A', 1), ('B', 1)], [('C', 1)]]},
                           {'branches': [[('D', 1)]]}]}]
    Lp, _ = compute_port_matrix(L4m, n4m, ports)
    assert abs(Lp[0, 0] - 2366 / 975 * 1e-6) < 1e-12, f'段间并联+支路失败: {Lp[0,0]}'

    # ---- 变压器 T 型等效 (n = Np/Ns) ----
    # (9) 耦合绕组 Lpp=2u, Lss=3u, M=1u (k=1/√6):
    #     n = √(2/3); Lm = n·M = √(2/3) µ; Lkp = 2−√(2/3) µ;
    #     Lks = Lss − M/n = 3 − √(3/2) µ;
    #     折算: Lm1 = M²/Lss = 1/3 µ; Lkp1 = 2 − 1/3 = 5/3 µ; n1 = M/Lss = 1/3
    L2p = np.array([[2e-6, 1e-6],
                    [1e-6, 3e-6]])
    nm2 = ['P', 'S']
    te = compute_t_equiv(L2p, nm2, 'P', 'S')
    assert abs(te['k'] - 1 / np.sqrt(6)) < 1e-12, f'k: {te["k"]}'
    assert abs(te['n'] - np.sqrt(2 / 3)) < 1e-12, f'n: {te["n"]}'
    assert abs(te['Lm'] - np.sqrt(2 / 3) * 1e-6) < 1e-18, f'Lm: {te["Lm"]}'
    assert abs(te['Lkp'] - (2 - np.sqrt(2 / 3)) * 1e-6) < 1e-18, f'Lkp: {te["Lkp"]}'
    assert abs(te['Lks'] - (3 - np.sqrt(3 / 2)) * 1e-6) < 1e-18, f'Lks: {te["Lks"]}'
    assert abs(te['Lm1'] - 1 / 3 * 1e-6) < 1e-18, f'Lm1: {te["Lm1"]}'
    assert abs(te['Lkp1'] - 5 / 3 * 1e-6) < 1e-18, f'Lkp1: {te["Lkp1"]}'
    assert abs(te['n1'] - 1 / 3) < 1e-12, f'n1: {te["n1"]}'

    # (10) 端口矩阵守恒: 用 T 型等效参数重建 2×2 矩阵必须等于原矩阵
    #     第一步模型 (n=Np/Ns): Lpp = Lkp+Lm, M = Lm/n, Lss = Lks+Lm/n²
    assert abs((te['Lkp'] + te['Lm']) - 2e-6) < 1e-18
    assert abs(te['Lm'] / te['n'] - 1e-6) < 1e-18
    assert abs(te['Lks'] + te['Lm'] / te['n'] ** 2 - 3e-6) < 1e-18
    #     第二步模型: Lpp = Lkp1+Lm1, M = Lm1/n1, Lss = Lm1/n1²
    assert abs((te['Lkp1'] + te['Lm1']) - 2e-6) < 1e-18
    assert abs(te['Lm1'] / te['n1'] - 1e-6) < 1e-18
    assert abs(te['Lm1'] / te['n1'] ** 2 - 3e-6) < 1e-18

    # (11) 反向耦合 M<0 (反极性): 匝比恒正, 漏感/励磁与正向一致, 负号仅表极性
    L2n = np.array([[2e-6, -1e-6],
                    [-1e-6, 3e-6]])
    ten = compute_t_equiv(L2n, nm2, 'P', 'S')
    assert ten['n'] > 0 and abs(ten['n'] - te['n']) < 1e-18, f'反相 n 应恒正: {ten["n"]}'
    assert abs(ten['Lm'] - te['Lm']) < 1e-18, f'反相 Lm: {ten["Lm"]}'
    assert abs(ten['Lkp'] - te['Lkp']) < 1e-18 and abs(ten['Lks'] - te['Lks']) < 1e-18
    assert ten['n1'] > 0 and abs(ten['n1'] - te['n1']) < 1e-18, f'反相 n1 应恒正: {ten["n1"]}'
    assert abs(ten['Lkp1'] - te['Lkp1']) < 1e-18

    # (12) 弱耦合 k→0: 漏感趋近自感, 励磁趋近 0
    Lwk = np.array([[2e-6, 1e-9],
                    [1e-9, 3e-6]])
    tew = compute_t_equiv(Lwk, nm2, 'P', 'S')
    assert tew['k'] < 1e-2 and tew['Lm'] < 1e-6 * 0.01, f'弱耦合 Lm: {tew["Lm"]}'

    # ---- 三端口多绕组等效 + 副边并联均流 ----
    # 3 端口: P 原边, S1/S2 副边 (对称设计)
    # Lpp=10u, Ls1=Ls2=2.5u (n=√(10/2.5)=2), Mp1=Mp2=4.5u, M12=1.0u
    L3 = np.array([[10e-6, 4.5e-6, 4.5e-6],
                   [4.5e-6, 2.5e-6, 1.0e-6],
                   [4.5e-6, 1.0e-6, 2.5e-6]])
    nm3 = ['P', 'S1', 'S2']

    # (13) 多绕组等效: 对称副边 n1=n2=2, k1=k2=4.5/5=0.9, Lks=2.5-4.5/2=0.25u
    mt = compute_multi_t_equiv(L3, nm3, 'P', ['S1', 'S2'])
    assert abs(mt['Lpp'] - 10e-6) < 1e-18
    assert abs(mt['n']['S1'] - 2.0) < 1e-12 and abs(mt['n']['S2'] - 2.0) < 1e-12
    assert abs(mt['k']['S1'] - 0.9) < 1e-12 and abs(mt['k']['S2'] - 0.9) < 1e-12
    assert abs(mt['Lks']['S1'] - 0.25e-6) < 1e-18
    assert abs(mt['Lks_ref']['S1'] - 1.0e-6) < 1e-18      # Lpp(1-k)=10·0.1=1u
    k12 = mt['k_sec'][('S1', 'S2')]
    assert abs(k12 - 1.0 / 2.5) < 1e-12, f'k12: {k12}'

    # (14) 对称副边并联带载: 均流 share1=share2=0.5, 不平衡度 0 (任何负载)
    for R in (0.0, 0.01, 1.0):
        es = eval_secondary_parallel(L3, nm3, 'P', 'S1', 'S2', load_r=R, freq=100e3)
        assert abs(es['share1'] - 0.5) < 1e-9 and abs(es['imbalance']) < 1e-9, f'对称均流 R={R}: {es}'

    # (15) 不对称副边 (Mp2 小): 均流偏差明显, 且 k12 增大时不平衡度减小
    L3u = L3.copy()
    L3u[0, 2] = L3u[2, 0] = 4.0e-6       # Mp2 = 4u (副边2 耦合弱)
    eu0 = eval_secondary_parallel(L3u, nm3, 'P', 'S1', 'S2', load_r=0.1, freq=100e3)
    assert abs(eu0['share1'] - 0.5) > 1e-2, f'不对称应不均流: {eu0["share1"]}'
    assert eu0['imbalance'] > 1e-2

    # (16) k12 扫描: 对称绕组 share1 恒为 0.5; 不对称绕组 share1 随 k12 变化
    sc_sym = scan_sec_coupling(L3, nm3, 'P', 'S1', 'S2', n_points=21, load_r=0.1, freq=100e3)
    assert len(sc_sym['k12']) == 21
    assert all(abs(s - 0.5) < 1e-9 for s in sc_sym['share1']), '对称绕组扫描应恒均流'
    sc_asym = scan_sec_coupling(L3u, nm3, 'P', 'S1', 'S2', n_points=21, load_r=0.1, freq=100e3)
    shares = sc_asym['share1']
    assert any(abs(s - 0.5) > 1e-2 for s in shares), '不对称绕组扫描应出现不均流'
    # k12 对均流有实质影响 (曲线两端差异明显); 方向由物理决定 (此处 Mp2 弱时强耦合使副边2 被拖动)
    assert abs(shares[-1] - shares[0]) > 1e-3, f'k12 应影响均流: {shares[0]} -> {shares[-1]}'
    # k12 上限受正定性约束 ≤ 1
    assert 0 < sc_asym['k12_max'] <= 1.0
    # 对称绕组短路 (R=0) 也应完全均流
    es0 = eval_secondary_parallel(L3, nm3, 'P', 'S1', 'S2', load_r=0.0, freq=100e3)
    assert abs(es0['share1'] - 0.5) < 1e-9

    print('全部自检通过 ✓')


if __name__ == '__main__':
    _selftest()
