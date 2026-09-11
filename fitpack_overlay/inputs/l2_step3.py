import os
conf={}

# =====================================================================
# JAM25 (arXiv:2510.11979v2) 从头复现卡 —— Phase A（π 侧实验数据）
# ---------------------------------------------------------------------
# 设计原则：本卡不复制 barryp 工作区的任何输入卡。每一个选择都依据
# 论文正文 / 引用的 JAM 公开文献 / 组内共享基础设施，出处见行内注释。
# 阶梯设计（论文未规定阶梯，此为我们的自研设计）：
#   step 1 : DY-pion (E615 + NA10, xF>=0)          → π PDF 9 参数
#   step 2 : + Leading Neutron (H1 + ZEUS, y<0.2)  → + LN 截断质量 λ
#   step 3 : + 格点矩 (ETMC n=1,2,3; 自写模块)      → Phase B
#   step 4 : + NA3 K/π DY 比值 (Q 4.1-8.5 GeV)     → + kaon PDF 9 参数 → Phase C
# 发表版配置：231 实验点 + 15 格点 = 246 点；25 自由参数（π9 + K9 + 归一6 + λ1）
# =====================================================================

#--fitting setups（Bayesian MC：flat par 平坦先验抽样 + posterior->prior 传递）
conf['bootstrap']=False
conf['flat par']=True
conf['ftol']=1e-8

#--setups for DGLAP（论文：NLO+NLL，输入标度 mu0 = m_c = 1.28 GeV）
conf['dglap mode']='truncated'
conf['alphaSmode']='backward'
conf['order']='NLO'
conf['mc']=1.28            # 论文 mu0 取 m_c 即 1.28 GeV（fitpack aux 默认 1.28）
conf['Q20']=1.28**2        # 注意：barryp 内部卡写的是 1.27**2，与论文不一致；本卡按论文取 1.28**2

#--DY 阈值重求和（论文：NLO+NLL 精度，双 Mellin 方法 [18,55]）
conf['pion resummation']=True
conf['MP method']='double mellin'

#--setups for IDIS and SIDIS（本 Phase 不用，保留标准开关）
conf['tmc']=False
conf['ht']=False
conf['nuc smearing']=True
conf['sidis nuc smearing']=False
conf['hq']=False

#--setup for LN（论文引文 [13] McKenney-Sato PRD93: 幺正 p->pi n 劈裂函数模型）
conf['ln mode']='model'
conf['SPLFUNC model']='IMF exp'
#--LeadingBaryon 依赖旧版 idis 的 _get_FXN → 强制旧 idis 接口（基础设施兼容补丁）
conf['dis version']=0

# =====================================================================
# 数据（组内共享 database；论文数据集清单见正文 Sec. "Experimental and
# lattice QCD constraints"）
# =====================================================================
conf['datasets']={}

##--DY-pion（论文：NA10 [7] + E615 [8] 共 117 点；截断 xF>=0 [7]，
##   标准 Q 范围同 JAM 公开文献 [15-20]）
conf['datasets']['dy-pion']={}
conf['datasets']['dy-pion']['filters']=[]
conf['datasets']['dy-pion']['filters'].append("Q2>4.16**2")
conf['datasets']['dy-pion']['filters'].append("Q2<59.0")
conf['datasets']['dy-pion']['filters'].append("xF>0")    # 论文：xF 非负截断
conf['datasets']['dy-pion']['filters'].append("xF<0.9")  # 高 xF 端重整化不确定区
conf['datasets']['dy-pion']['xlsx']={}
conf['datasets']['dy-pion']['xlsx'][10001]='dy-pion/expdata/10001.xlsx'  # E615, E 252 GeV, W 靶
conf['datasets']['dy-pion']['xlsx'][10002]='dy-pion/expdata/10002.xlsx'  # NA10, E 194 GeV
conf['datasets']['dy-pion']['xlsx'][10003]='dy-pion/expdata/10003.xlsx'  # NA10, E 286 GeV
conf['datasets']['dy-pion']['norm']={}
conf['datasets']['dy-pion']['norm'][10001]={'value':1.0,'fixed':False,'min':0.5,'max':1.5}
conf['datasets']['dy-pion']['norm'][10002]={'value':1.0,'fixed':False,'min':0.5,'max':1.5}
conf['datasets']['dy-pion']['norm'][10003]={'value':1.0,'fixed':False,'min':0.5,'max':1.5}

##--LN（论文：H1 [9] + ZEUS [10] 共 108 点；y<0.2 为 Sullivan 模型
##   有效性标准截断，同 JAM 公开文献 [15]）
conf['datasets']['ln']={}
conf['datasets']['ln']['filters']=[]
conf['datasets']['ln']['filters'].append("y<0.2")
conf['datasets']['ln']['xlsx']={}
conf['datasets']['ln']['xlsx'][1000]='ln/expdata/1000.xlsx'  # H1 F2LN(3)
conf['datasets']['ln']['xlsx'][2000]='ln/expdata/2000.xlsx'  # ZEUS R
conf['datasets']['ln']['norm']={}
conf['datasets']['ln']['norm'][1000]={'value':1.0,'fixed':False,'min':0.5,'max':1.5}
conf['datasets']['ln']['norm'][2000]={'value':1.0,'fixed':False,'min':0.5,'max':1.5}

##--LQCD moments（论文 Table 1：ETMC n=1 + Alexandrou n=2,3 误差x2；
##   自写模块 obslib/lqcd_mom，数据表由论文数值生成）
conf['datasets']['lqcd_mom']={}
conf['datasets']['lqcd_mom']['filters']=[]
conf['datasets']['lqcd_mom']['xlsx']={}
conf['datasets']['lqcd_mom']['xlsx'][1000]='lqcd_mom/expdata/1000.xlsx'  # kaon, 9 pts
conf['datasets']['lqcd_mom']['xlsx'][1001]='lqcd_mom/expdata/1001.xlsx'  # pion, 6 pts
conf['datasets']['lqcd_mom']['norm']={}

# =====================================================================
# 参数（论文 Methodology 节：Eq. (4)，f = N x^a (1-x)^b (1 + c x^d)，
#       π 9 参数 + K 9 参数 + 数据归一 6 + LN λ 1 = 25）
# =====================================================================
conf['params']={}

#--核子 PDF：固定为已发表 JAM 质子 PDF（基础设施输入，全部 fixed）
conf['params']['pdf']={}
conf['params']['pdf']['g1 N']    ={'value':    3.87592e-01, 'min':  None, 'max':  None, 'fixed': True }
conf['params']['pdf']['g1 a']    ={'value':   -6.23068169e-01, 'min':  -1.9, 'max':     1, 'fixed': True}
conf['params']['pdf']['g1 b']    ={'value':    9.25741583e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['uv1 N']   ={'value':    3.47549e-01, 'min':  None, 'max':  None, 'fixed': True }
conf['params']['pdf']['uv1 a']   ={'value':   -1.21835956e-01, 'min':  -0.5, 'max':     1, 'fixed': True}
conf['params']['pdf']['uv1 b']   ={'value':    3.20766744e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['dv1 N']   ={'value':    1.52089e-01, 'min':  None, 'max':  None, 'fixed': True }
conf['params']['pdf']['dv1 a']   ={'value':   -2.39874967e-01, 'min':  -0.5, 'max':     1, 'fixed': True}
conf['params']['pdf']['dv1 b']   ={'value':    3.83902620e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['db1 N']   ={'value':    3.67609928e-02, 'min':     0, 'max':     1, 'fixed': True}
conf['params']['pdf']['db1 a']   ={'value':   -8.41360631e-01, 'min':    -1, 'max':     1, 'fixed': True}
conf['params']['pdf']['db1 b']   ={'value':    5.31285539e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['ub1 N']   ={'value':    1.95464789e-02, 'min':     0, 'max':     1, 'fixed': True}
conf['params']['pdf']['ub1 a']   ={'value':    -9.93659187e-01, 'min':    -1, 'max':     1, 'fixed': True}
conf['params']['pdf']['ub1 b']   ={'value':    8.38905814e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['s1 N']    ={'value':    0.00000e+00, 'min':     0, 'max':     1, 'fixed': True }
conf['params']['pdf']['s1 a']    ={'value':   1.34706224e-01, 'min':  -0.5, 'max':     1, 'fixed': True}
conf['params']['pdf']['s1 b']    ={'value':    6.00759596e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['sb1 N']   ={'value':    7.46109845e-07, 'min':     0, 'max':     1, 'fixed': True}
conf['params']['pdf']['sb1 a']   ={'value':   3.83495317e-01, 'min':  -0.5, 'max':     1, 'fixed': True}
conf['params']['pdf']['sb1 b']   ={'value':    4.61209808e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['sea1 N']  ={'value':    5.71081196e-03, 'min':     0, 'max':     1, 'fixed': True}
conf['params']['pdf']['sea1 a']  ={'value':   -1.36329697e+00, 'min':  -1.9, 'max':    -1, 'fixed': True}
conf['params']['pdf']['sea1 b']  ={'value':    4.74721050e+00, 'min':     0, 'max':    10, 'fixed': True}
conf['params']['pdf']['sea2 N']  ={'value':    2.08640e-02, 'min':     0, 'max':     1, 'fixed': 'sea1 N'}
conf['params']['pdf']['sea2 a']  ={'value':   -1.500000000, 'min':  -1.9, 'max':    -1, 'fixed': 'sea1 a'}
conf['params']['pdf']['sea2 b']  ={'value':    1.00000e+01, 'min':     0, 'max':    10, 'fixed': 'sea1 b'}

#--π PDF（论文：电荷对称 ū^π- = d^π-；价夸克 N 由重子数、海夸克 N 由动量
#   求和规则固定；海/胶子 γ=δ=0。自由参数 = 价 a,b,c,d + 海 a,b + 胶子 N,a,b = 9）
#   初值/范围为我们的选择（从头起步，非抄自任何现有卡）
conf['params']['pdf-pion']={}
# 价夸克 ubv1（=dv1）：N 由求和规则计算（占位）
conf['params']['pdf-pion']['ubv1 N'] ={'value': 1.0, 'min': 0.0, 'max': 10.0, 'fixed':True}
conf['params']['pdf-pion']['ubv1 a'] ={'value': -0.5, 'min': -1.0, 'max':  1.0, 'fixed':False}
conf['params']['pdf-pion']['ubv1 b'] ={'value':  1.0, 'min':  0.0, 'max': 10.0, 'fixed':False}
conf['params']['pdf-pion']['ubv1 c'] ={'value':  0.0, 'min': -20.0, 'max': 20.0, 'fixed':False}
conf['params']['pdf-pion']['ubv1 d'] ={'value':  1.0, 'min':  0.1, 'max': 20.0, 'fixed':False}
# dv1 与 ubv1 绑定（电荷对称）
conf['params']['pdf-pion']['dv1 N']  ={'value': 1.0, 'min': 0.0, 'max': 10.0, 'fixed':'ubv1 N'}
conf['params']['pdf-pion']['dv1 a']  ={'value': -0.5, 'min': -1.0, 'max':  1.0, 'fixed':'ubv1 a'}
conf['params']['pdf-pion']['dv1 b']  ={'value':  1.0, 'min':  0.0, 'max': 10.0, 'fixed':'ubv1 b'}
conf['params']['pdf-pion']['dv1 c']  ={'value':  0.0, 'min': -20.0, 'max': 20.0, 'fixed':'ubv1 c'}
conf['params']['pdf-pion']['dv1 d']  ={'value':  1.0, 'min':  0.1, 'max': 20.0, 'fixed':'ubv1 d'}
# 海夸克 u1（=db1=s1=sb1）：N 由动量求和规则计算，γ=δ=0（论文）
conf['params']['pdf-pion']['u1 N']   ={'value': 0.1, 'min': 0.0, 'max': 10.0, 'fixed':True}
conf['params']['pdf-pion']['u1 a']   ={'value': -0.5, 'min': -1.0, 'max':  0.0, 'fixed':False}
conf['params']['pdf-pion']['u1 b']   ={'value':  5.0, 'min':  4.0, 'max': 12.0, 'fixed':False}
conf['params']['pdf-pion']['u1 c']   ={'value':  0.0, 'min': -5.0, 'max':  5.0, 'fixed':True}   # gamma0
conf['params']['pdf-pion']['u1 d']   ={'value':  0.0, 'min': -5.0, 'max':  5.0, 'fixed':True}   # delta0
conf['params']['pdf-pion']['db1 N']  ={'value': 0.1, 'min': 0.0, 'max': 10.0, 'fixed':'u1 N'}
conf['params']['pdf-pion']['db1 a']  ={'value': -0.5, 'min': -1.0, 'max':  0.0, 'fixed':'u1 a'}
conf['params']['pdf-pion']['db1 b']  ={'value':  5.0, 'min':  4.0, 'max': 12.0, 'fixed':'u1 b'}
conf['params']['pdf-pion']['db1 c']  ={'value':  0.0, 'fixed':'u1 c'}
conf['params']['pdf-pion']['db1 d']  ={'value':  0.0, 'fixed':'u1 d'}
conf['params']['pdf-pion']['s1 N']   ={'value': 0.1, 'min': 0.0, 'max': 10.0, 'fixed':'u1 N'}
conf['params']['pdf-pion']['s1 a']   ={'value': -0.5, 'min': -1.0, 'max':  0.0, 'fixed':'u1 a'}
conf['params']['pdf-pion']['s1 b']   ={'value':  5.0, 'min':  4.0, 'max': 12.0, 'fixed':'u1 b'}
conf['params']['pdf-pion']['s1 c']   ={'value':  0.0, 'fixed':'u1 c'}
conf['params']['pdf-pion']['s1 d']   ={'value':  0.0, 'fixed':'u1 d'}
conf['params']['pdf-pion']['sb1 N']  ={'value': 0.1, 'min': 0.0, 'max': 10.0, 'fixed':'u1 N'}
conf['params']['pdf-pion']['sb1 a']  ={'value': -0.5, 'min': -1.0, 'max':  0.0, 'fixed':'u1 a'}
conf['params']['pdf-pion']['sb1 b']  ={'value':  5.0, 'min':  4.0, 'max': 12.0, 'fixed':'u1 b'}
conf['params']['pdf-pion']['sb1 c']  ={'value':  0.0, 'fixed':'u1 c'}
conf['params']['pdf-pion']['sb1 d']  ={'value':  0.0, 'fixed':'u1 d'}
# 胶子 g1：γ=δ=0（论文）
conf['params']['pdf-pion']['g1 N']   ={'value':  0.5, 'min': 0.05, 'max': 1.5, 'fixed':False}
conf['params']['pdf-pion']['g1 a']   ={'value': -0.5, 'min': -1.9, 'max':  1.0, 'fixed':False}
conf['params']['pdf-pion']['g1 b']   ={'value':  5.0, 'min':  4.0, 'max': 12.0, 'fixed':False}
conf['params']['pdf-pion']['g1 c']   ={'value':  0.0, 'min': -5.0, 'max':  5.0, 'fixed':True}   # gamma0
conf['params']['pdf-pion']['g1 d']   ={'value':  0.0, 'min': -5.0, 'max':  5.0, 'fixed':True}   # delta0
conf['params']['pdf-pion']['strange_sea_scaling N']={'value': 1.0, 'fixed':True}

#--LN 截断质量（论文第 25 个参数；引文 [13]）
conf['params']['p->pi,n']={}
conf['params']['p->pi,n']['lambda']={'value': 1.0, 'min': 0.5, 'max': 2.0, 'fixed': False}

# =====================================================================
# 阶梯（我们的设计；maxlike 2026 一次一步，posterior->prior 链式传递）
# =====================================================================
conf['steps']={}

conf['steps'][1]={}
conf['steps'][1]['dep']=[]
conf['steps'][1]['active distributions']=['pdf-pion','pdf']
conf['steps'][1]['datasets']={}
conf['steps'][1]['datasets']['dy-pion']=[]
conf['steps'][1]['datasets']['dy-pion'].append(10001)
conf['steps'][1]['datasets']['dy-pion'].append(10002)
conf['steps'][1]['datasets']['dy-pion'].append(10003)

conf['steps'][2]={}
conf['steps'][2]['dep']=[1]
conf['steps'][2]['active distributions']=['pdf-pion','p->pi,n','pdf']
conf['steps'][2]['datasets']={}
conf['steps'][2]['datasets']['dy-pion']=[]
conf['steps'][2]['datasets']['dy-pion'].append(10001)
conf['steps'][2]['datasets']['dy-pion'].append(10002)
conf['steps'][2]['datasets']['dy-pion'].append(10003)
conf['steps'][2]['datasets']['ln']=[]
conf['steps'][2]['datasets']['ln'].append(1000)
conf['steps'][2]['datasets']['ln'].append(2000)

conf['steps'][3]={}
conf['steps'][3]['dep']=[2]
conf['steps'][3]['active distributions']=['pdf-pion','p->pi,n','pdf']
conf['steps'][3]['datasets']={}
conf['steps'][3]['datasets']['dy-pion']=[]
conf['steps'][3]['datasets']['dy-pion'].append(10001)
conf['steps'][3]['datasets']['dy-pion'].append(10002)
conf['steps'][3]['datasets']['dy-pion'].append(10003)
conf['steps'][3]['datasets']['ln']=[]
conf['steps'][3]['datasets']['ln'].append(1000)
conf['steps'][3]['datasets']['ln'].append(2000)
conf['steps'][3]['datasets']['lqcd_mom']=[]
conf['steps'][3]['datasets']['lqcd_mom'].append(1001)   # pion moments (6 pts)

# Phase B / Phase C 的 step 3、step 4 在各自阶段追加（见 paper_spec.md）
