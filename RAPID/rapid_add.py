#!/usr/bin/env python3
"""
RAPID (Rapid Automated Prefetcher Integration and Design) Evaluation Tool 
敏捷组合预取器自动化集成评估工具

简介:
    本脚本用于对多个子预取器（例如 stream/stride/cplx）的 PRT 与 stats 输出
    进行分阶段处理与汇总分析，支持并行化以加速大规模 trace 的处理流程。

主要流程:
    1) Phase1: 并行解析各子预取器的 PRT（Prefetch Request Trace），提取有用/无用
         地址及其使用次数；
    2) Phase2: 在 checkpoint 级别合并子预取器统计，生成面向 checkpoint 的汇总 JSON；
    3) Phase3: 基于子集交集结果并使用包含-排除原理，预测任意预取器组合的并集行为；
    4) Phase4: 读取真实的 PRT 与 stats，计算准确率/覆盖率等指标，并导出按
         checkpoint/workload/application 的 CSV 报表。

依赖:
    - Python 3.8+
    - pandas, tqdm

简要使用示例:
    python rapid.py --phase2-out phase2.json --phase3-out phase3.json --phase4-out-dir ./out --max-workers 8 -v

说明:
    请在脚本顶部调整 `FORMAT_PRT_FILE` 与 `FORMAT_STATS_FILE` 模板以匹配你的 Gem5 输出目录结构。
"""
import os
import re
import json
import ast
import itertools
import logging
from pathlib import Path
from typing import Dict
import concurrent.futures
from tqdm import tqdm
import pandas as pd

# ---------------------- 配置与常量（来自 vein_config.py） ----------------------
PREFETCHERS = ['stream', 'stride', 'cplx']

# 请根据你的输出目录调整以下路径模板
FORMAT_PRT_FILE   = "/nfs/home/qiuzeyuan/repos/GEM5/test/optimize/output/RAPID/mySpec06-full/L1/{}/{}/m5out/L1_PrefetchRequestTrace.txt"
FORMAT_STATS_FILE = "/nfs/home/qiuzeyuan/repos/GEM5/test/optimize/output/RAPID/mySpec06-full/L1/{}/{}/m5out/stats.txt"
ADDR_BASE = 16

CHECKPOINTS = ['astar_biglakes_10900_0.0744801', 'astar_biglakes_11643_0.109318', 'astar_biglakes_1861_0.0328103', 'astar_biglakes_2176_0.0936256', 'astar_biglakes_3532_0.103161', 'astar_biglakes_4153_0.0536827', 'astar_biglakes_4999_0.0885952', 'astar_biglakes_6261_0.0851415', 'astar_biglakes_7387_0.0367145', 'astar_biglakes_8794_0.0274795', 'astar_biglakes_9071_0.0348375', 'astar_biglakes_9104_0.0337112', 'astar_biglakes_982_0.0303326', 'astar_rivers_14611_0.200329', 'astar_rivers_14649_0.155733', 'astar_rivers_17235_0.0699332', 'astar_rivers_17251_0.0661937', 'astar_rivers_23651_0.0789851', 'astar_rivers_4474_0.125293', 'astar_rivers_5555_0.111278', 'bwaves_22203_0.697175', 'bwaves_3081_0.0401703', 'bwaves_52_0.0667232', 'bzip2_chicken_1852_0.0563963', 'bzip2_chicken_2059_0.0475776', 'bzip2_chicken_2083_0.0522591', 'bzip2_chicken_2284_0.193468', 'bzip2_chicken_3211_0.0844856', 'bzip2_chicken_4832_0.0480131', 'bzip2_chicken_778_0.091018', 'bzip2_chicken_8336_0.0628198', 'bzip2_chicken_8491_0.0698966', 'bzip2_chicken_9180_0.119325', 'bzip2_combined_10006_0.0574339', 'bzip2_combined_10434_0.0247425', 'bzip2_combined_1054_0.0551388', 'bzip2_combined_10754_0.10871', 'bzip2_combined_12343_0.0321317', 'bzip2_combined_12407_0.0853672', 'bzip2_combined_159_0.122481', 'bzip2_combined_17542_0.0256941', 'bzip2_combined_1812_0.0252463', 'bzip2_combined_2068_0.0544671', 'bzip2_combined_2722_0.0258061', 'bzip2_combined_35_0.0316279', 'bzip2_combined_4325_0.0794335', 'bzip2_combined_5497_0.0387371', 'bzip2_combined_6774_0.0246865', 'bzip2_combined_8000_0.031124', 'bzip2_html_11314_0.114627', 'bzip2_html_14435_0.294134', 'bzip2_html_16805_0.0819943', 'bzip2_html_20940_0.0754733', 'bzip2_html_22532_0.0261391', 'bzip2_html_3511_0.189715', 'bzip2_html_9318_0.0423454', 'bzip2_liberty_14098_0.053624', 'bzip2_liberty_14849_0.065606', 'bzip2_liberty_15268_0.0726118', 'bzip2_liberty_4594_0.0426242', 'bzip2_liberty_4624_0.328816', 'bzip2_liberty_5397_0.0579454', 'bzip2_liberty_6342_0.0841354', 'bzip2_liberty_8035_0.0749689', 'bzip2_liberty_9275_0.0504158', 'bzip2_program_14606_0.0844075', 'bzip2_program_16308_0.0605914', 'bzip2_program_18947_0.0223685', 'bzip2_program_19955_0.102812', 'bzip2_program_21478_0.0451851', 'bzip2_program_2335_0.0700007', 'bzip2_program_26679_0.0546633', 'bzip2_program_3524_0.087027', 'bzip2_program_3675_0.0666575', 'bzip2_program_5928_0.0530089', 'bzip2_program_7198_0.132488', 'bzip2_program_9855_0.03002', 'bzip2_source_15582_0.303805', 'bzip2_source_15845_0.0667498', 'bzip2_source_15868_0.0355138', 'bzip2_source_3738_0.0324392', 'bzip2_source_3897_0.0728099', 'bzip2_source_4559_0.0280724', 'bzip2_source_5298_0.0411728', 'bzip2_source_572_0.0313252', 'bzip2_source_64_0.0550753', 'bzip2_source_6777_0.105205', 'bzip2_source_7643_0.0473665', 'cactusADM_130635_0.151675', 'cactusADM_41713_0.779518', 'calculix_135177_0.233266', 'calculix_152041_0.205063', 'calculix_179055_0.0582832', 'calculix_70037_0.29005', 'calculix_91575_0.0514629', 'dealII_22706_0.0954695', 'dealII_24241_0.030989', 'dealII_29816_0.101304', 'dealII_31770_0.096517', 'dealII_34065_0.0332877', 'dealII_3427_0.031862', 'dealII_4038_0.0331859', 'dealII_43272_0.0372596', 'dealII_51313_0.0332586', 'dealII_59334_0.0829429', 'dealII_60735_0.0430791', 'dealII_60862_0.071042', 'dealII_7249_0.0427736', 'dealII_8693_0.0832194', 'gamess_cytosine_12113_0.0668627', 'gamess_cytosine_14041_0.0768822', 'gamess_cytosine_27470_0.0610638', 'gamess_cytosine_31435_0.0473389', 'gamess_cytosine_34804_0.0965555', 'gamess_cytosine_36002_0.0737251', 'gamess_cytosine_38284_0.0928834', 'gamess_cytosine_50720_0.108303', 'gamess_cytosine_53134_0.079657', 'gamess_cytosine_8749_0.101723', 'gamess_gradient_15940_0.0505566', 'gamess_gradient_18906_0.130292', 'gamess_gradient_21085_0.035651', 'gamess_gradient_23127_0.0772685', 'gamess_gradient_23715_0.0375274', 'gamess_gradient_25268_0.0457075', 'gamess_gradient_30568_0.0431987', 'gamess_gradient_34433_0.110242', 'gamess_gradient_41227_0.0858492', 'gamess_gradient_42410_0.0327205', 'gamess_gradient_46002_0.0349131', 'gamess_gradient_5288_0.0647453', 'gamess_gradient_6383_0.0534238', 'gamess_triazolium_121694_0.0556802', 'gamess_triazolium_12522_0.0595034', 'gamess_triazolium_13232_0.0693355', 'gamess_triazolium_136287_0.0471104', 'gamess_triazolium_139790_0.0378521', 'gamess_triazolium_14029_0.0510745', 'gamess_triazolium_186606_0.0381129', 'gamess_triazolium_28019_0.0818694', 'gamess_triazolium_39960_0.0333612', 'gamess_triazolium_45006_0.0507407', 'gamess_triazolium_452_0.0846495', 'gamess_triazolium_51410_0.102994', 'gamess_triazolium_77321_0.0380972', 'gamess_triazolium_84878_0.0640674', 'gcc_166_1569_0.0318317', 'gcc_166_1628_0.0588077', 'gcc_166_1903_0.225519', 'gcc_166_2269_0.149177', 'gcc_166_2389_0.0401942', 'gcc_166_2554_0.0593472', 'gcc_166_2573_0.0318317', 'gcc_166_3279_0.0480173', 'gcc_166_958_0.155921', 'gcc_200_1316_0.0393616', 'gcc_200_4297_0.0627621', 'gcc_200_4879_0.0259705', 'gcc_200_5029_0.0684431', 'gcc_200_5204_0.13878', 'gcc_200_5333_0.0266468', 'gcc_200_5692_0.0937373', 'gcc_200_6495_0.173543', 'gcc_200_7100_0.085351', 'gcc_200_741_0.0753415', 'gcc_200_810_0.026241', 'gcc_cpdecl_1093_0.0249148', 'gcc_cpdecl_1349_0.166099', 'gcc_cpdecl_1856_0.052385', 'gcc_cpdecl_2275_0.197615', 'gcc_cpdecl_4216_0.0587734', 'gcc_cpdecl_4243_0.0376917', 'gcc_cpdecl_4297_0.187394', 'gcc_cpdecl_498_0.0330068', 'gcc_cpdecl_535_0.0313032', 'gcc_cpdecl_591_0.0310903', 'gcc_expr2_1341_0.0323172', 'gcc_expr2_1836_0.132091', 'gcc_expr2_3279_0.16455', 'gcc_expr2_3879_0.110641', 'gcc_expr2_387_0.121789', 'gcc_expr2_5836_0.0369743', 'gcc_expr2_6143_0.0681626', 'gcc_expr2_7061_0.151002', 'gcc_expr_1532_0.188289', 'gcc_expr_2280_0.061615', 'gcc_expr_2587_0.0449675', 'gcc_expr_265_0.032147', 'gcc_expr_3015_0.0729047', 'gcc_expr_4350_0.0445848', 'gcc_expr_4352_0.0585534', 'gcc_expr_5114_0.171259', 'gcc_expr_694_0.12744', 'gcc_g23_127_0.182169', 'gcc_g23_1849_0.0327309', 'gcc_g23_3560_0.132136', 'gcc_g23_4432_0.0398942', 'gcc_g23_5572_0.0804496', 'gcc_g23_6410_0.044633', 'gcc_g23_6894_0.100397', 'gcc_g23_7781_0.0571964', 'gcc_g23_8973_0.134009', 'gcc_s04_1211_0.202775', 'gcc_s04_4078_0.0523815', 'gcc_s04_4507_0.0768846', 'gcc_s04_574_0.0421303', 'gcc_s04_6223_0.177397', 'gcc_s04_7210_0.0411301', 'gcc_s04_7659_0.0675084', 'gcc_s04_83_0.167396', 'gcc_scilab_1121_0.125347', 'gcc_scilab_2372_0.0715278', 'gcc_scilab_2542_0.0708333', 'gcc_scilab_2652_0.102083', 'gcc_scilab_272_0.0958333', 'gcc_scilab_48_0.151389', 'gcc_scilab_735_0.0427083', 'gcc_scilab_889_0.142361', 'gcc_typeck_1485_0.0369515', 'gcc_typeck_1846_0.114088', 'gcc_typeck_2846_0.0923788', 'gcc_typeck_3319_0.0976135', 'gcc_typeck_4195_0.0355658', 'gcc_typeck_4900_0.241724', 'gcc_typeck_6113_0.151039', 'gcc_typeck_6353_0.0537336', 'GemsFDTD_10352_0.026569', 'GemsFDTD_16528_0.0269638', 'GemsFDTD_24014_0.0330071', 'GemsFDTD_27913_0.274606', 'GemsFDTD_3488_0.0368999', 'GemsFDTD_3529_0.037771', 'GemsFDTD_42385_0.0308293', 'GemsFDTD_44911_0.0350624', 'GemsFDTD_49523_0.0337285', 'GemsFDTD_57450_0.203038', 'GemsFDTD_68636_0.0391049', 'GemsFDTD_72972_0.0275354', 'gobmk_13x13_10596_0.104333', 'gobmk_13x13_10649_0.128899', 'gobmk_13x13_1249_0.172509', 'gobmk_13x13_2406_0.095501', 'gobmk_13x13_5878_0.0771', 'gobmk_13x13_6753_0.0857485', 'gobmk_13x13_8086_0.107646', 'gobmk_13x13_9920_0.0678995', 'gobmk_nngs_12501_0.0714976', 'gobmk_nngs_15123_0.0938687', 'gobmk_nngs_19420_0.0897949', 'gobmk_nngs_20434_0.154837', 'gobmk_nngs_23825_0.120141', 'gobmk_nngs_3076_0.216875', 'gobmk_nngs_3195_0.101533', 'gobmk_score2_10042_0.0992273', 'gobmk_score2_1106_0.0495171', 'gobmk_score2_11398_0.062331', 'gobmk_score2_13389_0.0536381', 'gobmk_score2_1369_0.0619446', 'gobmk_score2_13706_0.128268', 'gobmk_score2_3471_0.0795879', 'gobmk_score2_42_0.0690277', 'gobmk_score2_5268_0.0721185', 'gobmk_score2_6537_0.103606', 'gobmk_score2_768_0.0618158', 'gobmk_trevorc_4051_0.253745', 'gobmk_trevorc_5292_0.0781447', 'gobmk_trevorc_772_0.119834', 'gobmk_trevorc_8503_0.128045', 'gobmk_trevorc_9426_0.146003', 'gobmk_trevorc_9575_0.0906876', 'gobmk_trevord_11723_0.138514', 'gobmk_trevord_12576_0.0515549', 'gobmk_trevord_14261_0.045728', 'gobmk_trevord_1501_0.0370511', 'gobmk_trevord_15433_0.102286', 'gobmk_trevord_333_0.055925', 'gobmk_trevord_359_0.044398', 'gobmk_trevord_4231_0.0790424', 'gobmk_trevord_4556_0.10235', 'gobmk_trevord_5918_0.142694', 'gobmk_trevord_8625_0.0357211', 'gromacs_11017_0.213766', 'gromacs_13813_0.0546897', 'gromacs_14849_0.142168', 'gromacs_22178_0.0315601', 'gromacs_31469_0.0218852', 'gromacs_53725_0.0765749', 'gromacs_56364_0.0330534', 'gromacs_57749_0.0713641', 'gromacs_59768_0.0659667', 'gromacs_64063_0.0206253', 'gromacs_991_0.0738684', 'h264ref_foreman.baseline_10924_0.0724533', 'h264ref_foreman.baseline_15966_0.0599338', 'h264ref_foreman.baseline_18495_0.100118', 'h264ref_foreman.baseline_20234_0.13859', 'h264ref_foreman.baseline_21432_0.0754595', 'h264ref_foreman.baseline_24358_0.0687621', 'h264ref_foreman.baseline_4315_0.0613418', 'h264ref_foreman.baseline_4362_0.0681913', 'h264ref_foreman.baseline_4826_0.111153', 'h264ref_foreman.baseline_9096_0.0647665', 'h264ref_foreman.main_10687_0.0477851', 'h264ref_foreman.main_11253_0.0484391', 'h264ref_foreman.main_11841_0.0553279', 'h264ref_foreman.main_12159_0.0555895', 'h264ref_foreman.main_124_0.064789', 'h264ref_foreman.main_14598_0.0744245', 'h264ref_foreman.main_14614_0.0587286', 'h264ref_foreman.main_15685_0.0586414', 'h264ref_foreman.main_15757_0.0504447', 'h264ref_foreman.main_20160_0.0504011', 'h264ref_foreman.main_2558_0.125044', 'h264ref_foreman.main_4794_0.0494855', 'h264ref_foreman.main_8200_0.0545867', 'h264ref_foreman.main_832_0.045954', 'h264ref_sss_121358_0.0543657', 'h264ref_sss_130026_0.0480866', 'h264ref_sss_158472_0.0778533', 'h264ref_sss_168790_0.0766516', 'h264ref_sss_24047_0.0510647', 'h264ref_sss_26367_0.0549262', 'h264ref_sss_30796_0.0537103', 'h264ref_sss_3425_0.0512072', 'h264ref_sss_37974_0.059809', 'h264ref_sss_58225_0.0582843', 'h264ref_sss_5836_0.0628013', 'h264ref_sss_85426_0.0477493', 'h264ref_sss_87139_0.0567549', 'h264ref_sss_87354_0.0498772', 'hmmer_nph3_2530_0.200789', 'hmmer_nph3_36408_0.1776', 'hmmer_nph3_43750_0.216784', 'hmmer_nph3_48802_0.145701', 'hmmer_nph3_6391_0.0994306', 'hmmer_retro_18076_0.0603529', 'hmmer_retro_21822_0.0700484', 'hmmer_retro_35648_0.0842565', 'hmmer_retro_39661_0.0777844', 'hmmer_retro_41583_0.0669947', 'hmmer_retro_49596_0.0501484', 'hmmer_retro_52872_0.0433285', 'hmmer_retro_56013_0.0531003', 'hmmer_retro_64606_0.0784375', 'hmmer_retro_71063_0.0875392', 'hmmer_retro_80967_0.0615828', 'hmmer_retro_91_0.0840784', 'lbm_12287_0.178129', 'lbm_578_0.636551', 'leslie3d_14058_0.0410965', 'leslie3d_14151_0.0208045', 'leslie3d_23371_0.166839', 'leslie3d_4103_0.162532', 'leslie3d_4615_0.0285353', 'leslie3d_4801_0.179226', 'leslie3d_51050_0.0254713', 'leslie3d_51602_0.0238139', 'leslie3d_54532_0.0525455', 'leslie3d_67737_0.0200085', 'leslie3d_71793_0.0207391', 'leslie3d_72680_0.0358627', 'leslie3d_84138_0.0297129', 'libquantum_28455_0.0635036', 'libquantum_30779_0.0590901', 'libquantum_32404_0.0458692', 'libquantum_52041_0.0488247', 'libquantum_52262_0.0475932', 'libquantum_67703_0.045219', 'libquantum_72935_0.0445491', 'libquantum_75068_0.0468938', 'libquantum_75470_0.052322', 'libquantum_77491_0.131135', 'libquantum_89770_0.0464997', 'libquantum_92707_0.177438', 'mcf_10104_0.0411966', 'mcf_11500_0.0885335', 'mcf_12098_0.134871', 'mcf_12884_0.0497644', 'mcf_4696_0.11645', 'mcf_6155_0.108953', 'mcf_6349_0.0435528', 'mcf_6835_0.0472655', 'mcf_8640_0.0549764', 'mcf_8770_0.0513351', 'mcf_9617_0.097244', 'milc_10442_0.0580394', 'milc_11233_0.031798', 'milc_14427_0.0966468', 'milc_22041_0.0506199', 'milc_23378_0.0406308', 'milc_2367_0.064656', 'milc_2419_0.0337573', 'milc_28653_0.0734567', 'milc_29361_0.0525149', 'milc_30021_0.0622471', 'milc_6469_0.0548918', 'milc_8233_0.0337252', 'milc_8749_0.101368', 'milc_9070_0.0595812', 'namd_17233_0.0558197', 'namd_22315_0.105221', 'namd_26584_0.121595', 'namd_34316_0.0588649', 'namd_35045_0.0467897', 'namd_52579_0.0492375', 'namd_54320_0.0690192', 'namd_56407_0.0518259', 'namd_69349_0.0430419', 'namd_75154_0.0544143', 'namd_75283_0.0564405', 'namd_76435_0.0487456', 'namd_81170_0.0590757', 'omnetpp_10752_0.092421', 'omnetpp_11416_0.0691041', 'omnetpp_15533_0.150057', 'omnetpp_19017_0.0578901', 'omnetpp_2340_0.134738', 'omnetpp_3352_0.143329', 'omnetpp_6608_0.138081', 'omnetpp_7121_0.0610215', 'perlbench_checkspam_1936_0.0520873', 'perlbench_checkspam_25383_0.0543488', 'perlbench_checkspam_26394_0.0696686', 'perlbench_checkspam_29168_0.0602944', 'perlbench_checkspam_31887_0.132024', 'perlbench_checkspam_35585_0.050756', 'perlbench_checkspam_36778_0.0662581', 'perlbench_checkspam_43746_0.0744652', 'perlbench_checkspam_49930_0.0756324', 'perlbench_checkspam_50305_0.0825628', 'perlbench_checkspam_7787_0.107074', 'perlbench_diffmail_14382_0.241631', 'perlbench_diffmail_6075_0.575225', 'perlbench_splitmail_1359_0.0430657', 'perlbench_splitmail_15799_0.174805', 'perlbench_splitmail_30298_0.558052', 'perlbench_splitmail_3399_0.0386321', 'povray_11592_0.096608', 'povray_12372_0.0859357', 'povray_13209_0.0746137', 'povray_13433_0.0927799', 'povray_14021_0.0912023', 'povray_1504_0.0447311', 'povray_15913_0.0525498', 'povray_25095_0.0945664', 'povray_26880_0.0398357', 'povray_30404_0.0499745', 'povray_4389_0.0616677', 'povray_5268_0.0403462', 'sjeng_112264_0.0903171', 'sjeng_12184_0.113042', 'sjeng_23309_0.141737', 'sjeng_24467_0.116833', 'sjeng_47715_0.0804572', 'sjeng_60448_0.119471', 'sjeng_70483_0.102456', 'sjeng_77065_0.0642522', 'soplex_pds-50_10346_0.0772972', 'soplex_pds-50_15496_0.0988941', 'soplex_pds-50_17226_0.0984888', 'soplex_pds-50_4942_0.0775867', 'soplex_pds-50_5684_0.107058', 'soplex_pds-50_5698_0.10729', 'soplex_pds-50_7372_0.064038', 'soplex_pds-50_7566_0.11858', 'soplex_pds-50_9237_0.0984888', 'soplex_ref_13231_0.0379228', 'soplex_ref_15257_0.307834', 'soplex_ref_3388_0.0534718', 'soplex_ref_353_0.0738872', 'soplex_ref_4098_0.0649258', 'soplex_ref_4427_0.039822', 'soplex_ref_5021_0.052997', 'soplex_ref_5381_0.0424332', 'soplex_ref_5801_0.0402374', 'soplex_ref_5864_0.0328783', 'soplex_ref_6899_0.0325816', 'soplex_ref_7707_0.0468249', 'sphinx3_131245_0.0941109', 'sphinx3_152496_0.109601', 'sphinx3_15704_0.0524937', 'sphinx3_167203_0.0788364', 'sphinx3_20636_0.0789874', 'sphinx3_24511_0.0537603', 'sphinx3_38865_0.0885043', 'sphinx3_61839_0.0700225', 'sphinx3_699_0.0702143', 'sphinx3_87390_0.0646134', 'sphinx3_96737_0.0668735', 'tonto_106969_0.0610062', 'tonto_11101_0.0302265', 'tonto_16759_0.0327069', 'tonto_17434_0.0438275', 'tonto_19174_0.075935', 'tonto_40077_0.0328729', 'tonto_43566_0.0367549', 'tonto_52207_0.101708', 'tonto_55355_0.0404433', 'tonto_59611_0.0514532', 'tonto_6051_0.0626568', 'tonto_66875_0.0550125', 'tonto_69048_0.0483089', 'tonto_69464_0.031001', 'tonto_73844_0.0762024', 'tonto_86391_0.0345788', 'wrf_107041_0.0469891', 'wrf_115155_0.0550117', 'wrf_124950_0.254161', 'wrf_133819_0.075861', 'wrf_135115_0.035247', 'wrf_135799_0.039022', 'wrf_2077_0.0394878', 'wrf_22055_0.0400996', 'wrf_44036_0.0601285', 'wrf_58475_0.0460227', 'wrf_65724_0.0318127', 'wrf_8158_0.0335437', 'wrf_95163_0.0482126', 'xalancbmk_10850_0.0638828', 'xalancbmk_11129_0.0306953', 'xalancbmk_14317_0.0291949', 'xalancbmk_1490_0.0176746', 'xalancbmk_15706_0.0299578', 'xalancbmk_17130_0.0244647', 'xalancbmk_19026_0.0189461', 'xalancbmk_19529_0.0264992', 'xalancbmk_21338_0.0262449', 'xalancbmk_22563_0.0203448', 'xalancbmk_23263_0.0302121', 'xalancbmk_24483_0.0183104', 'xalancbmk_25972_0.0303138', 'xalancbmk_26498_0.0195311', 'xalancbmk_29508_0.0176492', 'xalancbmk_30541_0.0193276', 'xalancbmk_31320_0.0258125', 'xalancbmk_32222_0.0210569', 'xalancbmk_33280_0.0210061', 'xalancbmk_34840_0.0199888', 'xalancbmk_35935_0.0198617', 'xalancbmk_36741_0.0166065', 'xalancbmk_37584_0.0572707', 'xalancbmk_37741_0.0179035', 'xalancbmk_38030_0.0179798', 'xalancbmk_39161_0.0222522', 'xalancbmk_3924_0.0503026', 'xalancbmk_5134_0.0428259', 'xalancbmk_557_0.0222522', 'xalancbmk_8178_0.0224811', 'zeusmp_1189_0.0313105', 'zeusmp_14657_0.0321822', 'zeusmp_177_0.0291521', 'zeusmp_1867_0.131413', 'zeusmp_24776_0.108611', 'zeusmp_26455_0.0681416', 'zeusmp_44878_0.0317671', 'zeusmp_48671_0.0557723', 'zeusmp_49170_0.0302313', 'zeusmp_49845_0.0438043', 'zeusmp_57543_0.0618601', 'zeusmp_64810_0.0660247', 'zeusmp_67847_0.131579']

WORKLOADS = sorted(list(set(['_'.join(checkpoint.split("_")[0:-2]) for checkpoint in CHECKPOINTS])), key=str.lower)
APPLICATIONS = sorted(list(set([checkpoint.split("_")[0] for checkpoint in CHECKPOINTS])), key=str.lower)

# 所有非空子预取器组合
TOTAL_COMBINATIONS = [c for i in range(1, len(PREFETCHERS) + 1) for c in itertools.combinations(PREFETCHERS, i)]

# 预编译正则
PATTERNS = {
    "useful":  re.compile(r"^\s*((?:0x)?[0-9a-fA-F]+)\s+([1-9]\d*)\s*$", re.MULTILINE),
    "useless": re.compile(r"^\s*((?:0x)?[0-9a-fA-F]+)\s+(0)\s*$", re.MULTILINE),
    "demand":  re.compile(r"system\.cpu\.dcache\.demandAccesses::total\s+(\d+)", re.MULTILINE),
}

# Phase4 输出字段常量，减少重复
PHASE4_INT_COLS = [
    "predict_useful", "real_useful",
    "predict_useless", "real_useless",
    "predict_useCount", "real_useCount",
    "demand",
]

PHASE4_PCT_COLS = [
    "predict_accuracy", "real_accuracy", "error_accuracy",
    "predict_coverage", "real_coverage", "error_coverage",
    "predict_coverage_2", "real_coverage_2", "error_coverage_2",
]

def parseArgs():
    import argparse, textwrap
    desc = textwrap.dedent("""
    RAPID: 分阶段预取器评估与统计汇总工具。
    脚本执行四个主要阶段：
      1) Phase1：解析各子预取器的 PRT 文件，统计有用/无用预取地址及其使用次数；
      2) Phase2：合并各 checkpoint 的子预取器统计，生成面向 checkpoint 的汇总数据；
      3) Phase3：基于交集数据并使用容斥原理，预测任意预取器组合的并集行为；
      4) Phase4：读取真实的 PRT 与 stats 文件，计算准确率/覆盖率指标并导出按 checkpoint、workload、application 的 CSV 报表。
    脚本支持并行处理以加速大规模 trace 分析，输出 JSON 与 CSV 文件，并通过命令行参数控制输出路径、并发工人数和详细日志级别。
    """)
    parser = argparse.ArgumentParser(description=desc, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase2-out", default="result_phase2_stats.json",
                        help="Phase2 输出 JSON 文件路径，默认: result_phase2_stats.json")
    parser.add_argument("--phase3-out", default="result_phase3_stats.json",
                        help="Phase3 输出 JSON 文件路径，默认: result_phase3_stats.json")
    parser.add_argument("--phase4-out-dir", default=".",
                        help="Phase4 输出 CSV 目录路径，默认: 当前目录")
    parser.add_argument("--max-workers", type=int, default=None,
                        help="并行处理的最大工作进程数，默认: CPU 核心数")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="启用详细日志输出")
    return parser.parse_args()


def setupLogger(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger("RAPID")
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger

def getLogger() -> logging.Logger:
    return logging.getLogger("RAPID")


def normalizeComb(comb) -> str:
    if isinstance(comb, (list, tuple)):
        return "_".join(str(x) for x in comb if x is not None and str(x).strip() != "")
    try:
        parsed = ast.literal_eval(comb)
        if isinstance(parsed, (list, tuple)):
            return "_".join(str(x) for x in parsed if x is not None and str(x).strip() != "")
    except Exception:
        pass
    parts = re.findall(r"\w+", str(comb))
    return "_".join(parts)


# ---------------------- stage1: 解析 PRT ----------------------

def _phase1ParsePRT(task: tuple, logger: logging.Logger = None) -> tuple:
    """解析单个 PRT 文件。

    返回 (sub_prefetcher, checkpoint, useful_dict, useless_dict, useCount_dict, not_found_flag)
    """
    from collections import defaultdict

    if logger is None:
        logger = getLogger()

    sub_prefetcher, checkpoint, file_path, addr_base = task
    useful = defaultdict(int)
    useless = defaultdict(int)
    useCount = defaultdict(int)

    patterns = PATTERNS
    useful_pat = patterns.get("useful")
    useless_pat = patterns.get("useless")
    iint = int
    logger.debug("_phase1ParsePRT start task: %s", str((sub_prefetcher, checkpoint, file_path)))

    try:
        with open(file_path) as fh:
            for line in fh:
                try:
                    m = useful_pat.match(line) if useful_pat is not None else None
                    if m:
                        pf_addr = iint(m.group(1), addr_base)
                        uc = iint(m.group(2))
                    elif useless_pat is not None:
                        m2 = useless_pat.match(line)
                        if m2:
                            pf_addr = iint(m2.group(1), addr_base)
                            uc = iint(m2.group(2))
                        else:
                            continue
                    else:
                        continue
                except Exception:
                    logger.debug("Malformed line in %s: %s", file_path, line.strip())
                    continue

                if uc > 0:
                    useful[pf_addr] += 1
                    useCount[pf_addr] += uc
                else:
                    useless[pf_addr] += 1

        logger.debug("Parsed PRT %s: useful=%d, useless=%d, useCount_entries=%d",
                 file_path, len(useful), len(useless), len(useCount))
        return (sub_prefetcher, checkpoint, dict(useful), dict(useless), dict(useCount), False)
    except FileNotFoundError:
        return (sub_prefetcher, checkpoint, {}, {}, {}, True)
    except Exception:
        logger.exception("Error parsing PRT %s", file_path)
        return (sub_prefetcher, checkpoint, {}, {}, {}, True)


def phase1ParsePRT(max_workers: int = None, logger: logging.Logger = None):
    """并行解析所有 PRT 文件以构建 trace 数据。

    使用 `executor.map` 和 `tqdm` 高效收集子任务结果。
    返回结构化的 trace 字典，格式为 trace[pre][checkpoint][{'useful','useless','useCount'}]
    """
    trace = {}
    for sub_prefetcher in PREFETCHERS:
        trace[sub_prefetcher] = {
            checkpoint: {'useful': {}, 'useless': {}, 'useCount': {}}
            for checkpoint in CHECKPOINTS
        }

    tasks = [(sub_prefetcher, checkpoint, FORMAT_PRT_FILE.format(sub_prefetcher, checkpoint), ADDR_BASE)
             for sub_prefetcher in PREFETCHERS for checkpoint in CHECKPOINTS]

    if max_workers is None:
        max_workers = min(64, (os.cpu_count() or 1))
    else:
        max_workers = min(max_workers, (os.cpu_count() or 1))

    getLogger().info("Phase1: preparing to parse PRT files; total tasks=%d, max_workers=%s", len(tasks), max_workers)
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exe:
        results = exe.map(_phase1ParsePRT, tasks)
        for res in tqdm(results, total=len(tasks), desc="Parsing PRT files"):
            try:
                sub_prefetcher, checkpoint, useful, useless, useCount, not_found = res
            except Exception:
                # 不应该发生，但为了健壮性处理
                getLogger().exception("Unexpected parse result: %s", res)
                continue

            trace[sub_prefetcher][checkpoint]['useful'] = useful
            trace[sub_prefetcher][checkpoint]['useless'] = useless
            trace[sub_prefetcher][checkpoint]['useCount'] = useCount

    getLogger().info("Phase1: finished parsing PRT files")
    return trace


# ---------------------- Phase2: 合并各检查点PRT ----------------------
def phase2Merge(trace, logger: logging.Logger = None):   
    """合并每个 checkpoint 中各子预取器的统计值，返回汇总结果。

    输入: trace（由 phase1ParsePRT 产生），输出: result[checkpoint][prefetcher] = {useful, useless, useCount}
    """

    if logger is None:
        logger = getLogger()

    logger.info("Phase2: merging checkpoint-level statistics")
    result = {
        checkpoint: {
            prefetcher: {
                  'useful': sum(trace[prefetcher][checkpoint]['useful'].values()), 
                  'useless': sum(trace[prefetcher][checkpoint]['useless'].values()), 
                  'useCount': sum(trace[prefetcher][checkpoint]['useCount'].values())
                }
            for prefetcher in PREFETCHERS
        } for checkpoint in CHECKPOINTS
    }

    logger.debug("Phase2: merged %d checkpoints", len(result))
    return result


# ---------------------- Phase3: 预测组合结果 ----------------------

def _phase3VeinInterWorker(task, logger: logging.Logger = None):
    """进程池中用于计算单个 (checkpoint, combination) 交集值的工作函数。

    输入: (checkpoint, combination, prefetch_subset)
    输出: (checkpoint, comb_str, {"useful": val, "useless": val, "useCount": val})
    """
    checkpoint, combination, prefetch_subset = task
    getLogger().debug("_phase3VeinInterWorker: checkpoint=%s comb=%s", checkpoint, normalizeComb(combination))
    dicts_useful = [prefetch_subset[sub]['useful'] for sub in combination]
    dicts_useless = [prefetch_subset[sub]['useless'] for sub in combination]
    dicts_useCount = [prefetch_subset[sub]['useCount'] for sub in combination]

    sums = {}
    for key, dicts in (('useful', dicts_useful), ('useless', dicts_useless), ('useCount', dicts_useCount)):
        if not dicts:
            sums[key] = 0
            continue
        inter = set(dicts[0].keys())
        for d in dicts[1:]:
            inter &= d.keys()
        total = 0
        for addr in inter:
            total += min(d.get(addr, 0) for d in dicts)
        sums[key] = total

    comb_str = normalizeComb(combination)
    return (checkpoint, comb_str, {"useful": sums['useful'], "useless": sums['useless'], "useCount": sums['useCount']})


def _phase3VeinInter(trace, max_workers: int = None, logger: logging.Logger = None):
    # 并行计算每个 (checkpoint, combination) 的交集值
    result = {
        checkpoint: {
            normalizeComb(comb): {"useful": 0, "useless": 0, "useCount": 0}
            for comb in TOTAL_COMBINATIONS
        } for checkpoint in CHECKPOINTS
    }
    tasks = [
        (checkpoint, combination, {sub: trace[sub][checkpoint] for sub in combination})
        for combination in TOTAL_COMBINATIONS
        for checkpoint in CHECKPOINTS
    ]
    
    if not tasks:
        return

    if max_workers is None:
        max_workers = min(64, (os.cpu_count() or 1))
    else:
        max_workers = min(max_workers, (os.cpu_count() or 1))

    getLogger().info("Phase3 (inter): computing intersections; total tasks=%d, max_workers=%d", len(tasks), max_workers)
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exe:
        for cp, comb_str, vals in tqdm(exe.map(_phase3VeinInterWorker, tasks), total=len(tasks), desc="Computing intersection (checkpoint,comb)"):
            result[cp][comb_str] = vals

    getLogger().info("Phase3 (inter): completed intersections")
    return result


def _phase3VeinUnion(intersection, logger: logging.Logger = None):

    """基于各子预取器交集数据，使用包含-排除原理计算任意组合的并集预测值。

    输入: intersection: data[checkpoint][subset_str] = {useful, useless, useCount}
    输出: result[checkpoint][comb_str] = {useful, useless, useCount}
    """

    if logger is None:
        logger = getLogger()

    logger.info("Phase3 (union): computing union via inclusion-exclusion")
    data = intersection

    result = {}
    for i in range(1, len(PREFETCHERS) + 1):
        combs = list(itertools.combinations(PREFETCHERS, i))
        for checkpoint in CHECKPOINTS:
            result.setdefault(checkpoint, {})
            for combination in combs:
                comb_str = "_".join(combination)
                useful = 0
                useless = 0
                useCount = 0

                # 初始累加单个子预取器的值
                for sub in combination:
                    useful += data[checkpoint].get(sub, {}).get("useful", 0)
                    useless += data[checkpoint].get(sub, {}).get("useless", 0)
                    useCount += data[checkpoint].get(sub, {}).get("useCount", 0)

                # 包含-排除：处理组合的子集
                comb_list = list(combination)
                for k in range(2, len(comb_list) + 1):
                    for sub_subset in itertools.combinations(comb_list, k):
                        subset_str = "_".join(sub_subset)
                        sign = -1 if (len(sub_subset) % 2 == 0) else 1
                        useful += sign * data[checkpoint].get(subset_str, {}).get("useful", 0)
                        useless += sign * data[checkpoint].get(subset_str, {}).get("useless", 0)
                        useCount += sign * data[checkpoint].get(subset_str, {}).get("useCount", 0)

                result[checkpoint][comb_str] = {"useful": useful, "useless": useless, "useCount": useCount}

    logger.debug("Phase3 (union): computed union for %d checkpoints", len(result))
    return result


def _phase3DirectSumCheckpointWorker(task, logger: logging.Logger = None):
    """并行 worker：计算单个 checkpoint 下所有组合的直接累加预测值。"""
    from collections import defaultdict

    checkpoint, checkpoint_trace = task
    checkpoint_result = {}

    for combination in TOTAL_COMBINATIONS:
        comb_str = normalizeComb(combination)

        useful_by_addr = defaultdict(int)
        useless_by_addr = defaultdict(int)
        usecount_by_addr = defaultdict(int)

        for sub in combination:
            sub_data = checkpoint_trace.get(sub, {})

            for addr, cnt in sub_data.get("useful", {}).items():
                useful_by_addr[addr] += cnt
            for addr, cnt in sub_data.get("useless", {}).items():
                useless_by_addr[addr] += cnt
            for addr, cnt in sub_data.get("useCount", {}).items():
                usecount_by_addr[addr] += cnt

        checkpoint_result[comb_str] = {
            "useful": sum(useful_by_addr.values()),
            "useless": sum(useless_by_addr.values()),
            "useCount": sum(usecount_by_addr.values()),
        }

    return checkpoint, checkpoint_result


def phase3Predict(trace, max_workers: int = None, logger: logging.Logger = None):
    """基于组合内各子预取器逐地址直接累加来预测组合结果。

    说明:
    - 不再使用交集 + 容斥原理估算并集；
    - 对于每个 (checkpoint, combination)，将组合中每个子预取器在每个地址上的
      useful/useless/useCount 直接相加；
    - 最终返回与原 Phase3 相同的聚合结构:
      result[checkpoint][comb_str] = {"useful": int, "useless": int, "useCount": int}
    """
    if logger is None:
        logger = getLogger()

    result = {checkpoint: {} for checkpoint in CHECKPOINTS}

    tasks = [
        (checkpoint, {sub: trace.get(sub, {}).get(checkpoint, {}) for sub in PREFETCHERS})
        for checkpoint in CHECKPOINTS
    ]

    if max_workers is None:
        max_workers = min(64, (os.cpu_count() or 1))
    else:
        max_workers = min(max_workers, (os.cpu_count() or 1))

    logger.info(
        "Phase3: predicting by direct per-address summation; total checkpoints=%d, max_workers=%d",
        len(tasks),
        max_workers,
    )

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as exe:
        for checkpoint, checkpoint_result in tqdm(
            exe.map(_phase3DirectSumCheckpointWorker, tasks),
            total=len(tasks),
            desc="Phase3 direct-sum (by checkpoint)",
        ):
            result[checkpoint] = checkpoint_result

    logger.info("Phase3: direct-summation prediction complete")
    return result


# ---------------------- Phase4: 比较并输出 CSV ----------------------
def loadRealVals(prtPath: Path, statsPath: Path, logger: logging.Logger = None) -> Dict[str, int]:
    """从 PRT 与 stats 文件中提取真实值计数。

    返回字典: {"useful": int, "useless": int, "useCount": int, "demand": int}
    prtPath: PRT 路径（预取跟踪），statsPath: stats.txt 路径（用于提取 demand）。
    """
    if logger is None:
        logger = getLogger()

    def sum_matches(s: str, pat: re.Pattern) -> int:
        if not s:
            return 0
        return sum(int(m.group(1)) for m in (pat.finditer(s) if (pat.flags & re.MULTILINE) else re.finditer(pat.pattern, s, pat.flags | re.MULTILINE)))

    def trace_count(s: str, pat: re.Pattern, countSum: bool = False) -> int:
        if not s:
            return 0
        if pat.flags & re.MULTILINE:
            iterator = pat.finditer(s)
        else:
            iterator = re.finditer(pat.pattern, s, pat.flags | re.MULTILINE)
        if countSum:
            # 使用匹配对象的最后一个捕获组作为数值
            return sum(int(m.group(m.lastindex)) for m in iterator)
        else:
            return sum(1 for _ in iterator)

    patterns = PATTERNS

    if not statsPath or not statsPath.exists():
        logger.debug("stats 文件缺失: %s", statsPath)
        return {"useful": 0, "useless": 0, "useCount": 0, "demand": 0}
    try:
        stats = statsPath.read_text()
    except Exception:
        logger.exception("读取 stats 失败 %s", statsPath)
        return {"useful": 0, "useless": 0, "useCount": 0, "demand": 0}
    demandCount = sum_matches(stats, patterns["demand"])

    if not prtPath or not prtPath.exists():
        logger.debug("PRT 文件缺失: %s", prtPath)
        return {"useful": 0, "useless": 0, "useCount": 0, "demand": demandCount}
    try:
        prt = prtPath.read_text()
    except Exception:
        logger.exception("读取 PRT 失败 %s", prtPath)
        return {"useful": 0, "useless": 0, "useCount": 0, "demand": 0}
    useCount = trace_count(prt, patterns["useful"], countSum=True)
    usefulCount = trace_count(prt, patterns["useful"])
    uselessCount = trace_count(prt, patterns["useless"])

    logger.debug("loadRealVals: %s -> useful=%d useless=%d useCount=%d demand=%d",
                 prtPath, usefulCount, uselessCount, useCount, demandCount)
    return {"useful": usefulCount, "useless": uselessCount, "useCount": useCount, "demand": demandCount}


def _process_task_for_counts(task, logger: logging.Logger = None):
    """辅助函数：在进程池中用于读取单个 (checkpoint, comb) 的 PRT 与 stats 并返回真实值。

    返回 (checkpoint, comb_str, result_dict)
    """
    cp, comb_str, prt_path_str, stats_path_str = task
    try:
        result = loadRealVals(Path(prt_path_str), Path(stats_path_str), None)
    except Exception:
        getLogger().exception("解析文件失败 %s %s", cp, comb_str)
        result = {"useful": 0, "useless": 0, "useCount": 0, "demand": 0}
    return (cp, comb_str, result)


def loadCkptRealValue(checkpoints_list, max_workers: int = None, logger: logging.Logger = None):
    """读取多个 checkpoint 下所有组合的真实值（PRT + stats），并计算 checkpoint 权重。

    返回 (checkpoint_realVals, checkpoint_weights)。
    checkpoint_realVals[checkpoint][comb_str] = {useful,useless,useCount,demand}
    checkpoint_weights[checkpoint] = 从 checkpoint 名称解析出的权重（若解析失败为 0.0）。
    """
    if logger is None:
        logger = getLogger()

    checkpoint_realVals = {}
    checkpoint_weights = {}
    tasks = []
    for checkpoint in checkpoints_list:
        checkpoint_realVals.setdefault(checkpoint, {})
        for comb in TOTAL_COMBINATIONS:
            comb_str = normalizeComb(comb)
            prt_path = Path(FORMAT_PRT_FILE.format(comb_str, checkpoint))
            stats_path = Path(FORMAT_STATS_FILE.format(comb_str, checkpoint))
            tasks.append((checkpoint, comb_str, str(prt_path), str(stats_path)))

    logger.info("Phase4: reading real values for checkpoints; total tasks=%d", len(tasks))
    if tasks:
        if max_workers is None:
            max_workers = min(64, (os.cpu_count() or 1))
        else:
            max_workers = min(max_workers, (os.cpu_count() or 1))
        logger.debug("Phase4: using max_workers=%d for reading real values", max_workers)
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as ex:
            for cp, comb_str, result in tqdm(ex.map(_process_task_for_counts, tasks), total=len(tasks), desc="读取跟踪文件"):
                checkpoint_realVals[cp][comb_str] = result

    for checkpoint in checkpoints_list:
        try:
            checkpoint_weights[checkpoint] = float(checkpoint.split("_")[-1])
        except Exception:
            checkpoint_weights[checkpoint] = 0.0

    logger.debug("Phase4: checkpoint weights sample: %s", dict(list(checkpoint_weights.items())[:5]))
    logger.info("Phase4: loaded real values for %d checkpoints", len(checkpoint_realVals))
    return checkpoint_realVals, checkpoint_weights


def computeMetrics(predictEntry: dict, realEntry: dict, logger: logging.Logger = None) -> dict: 
    """根据预测与真实条目计算一组评估指标（准确率、覆盖率及误差）。"""
    def _safeDiv(numerator, denominator):
      try:
          return numerator / denominator if denominator else 0
      except Exception:
          return 0

    pUseful = predictEntry.get("useful", 0)
    pUseless = predictEntry.get("useless", 0)
    pUseCount = predictEntry.get("useCount", 0)

    rUseful = realEntry.get("useful", 0)
    rUseless = realEntry.get("useless", 0)
    rUseCount = realEntry.get("useCount", 0)
    rDemand = realEntry.get("demand", 0)

    pAccuracy = _safeDiv(pUseful, (pUseful + pUseless))
    pCoverage = _safeDiv(pUseful, rDemand)
    pCoverage_ = _safeDiv(pUseCount, rDemand)

    rAccuracy = _safeDiv(rUseful, (rUseful + rUseless))
    rCoverage = _safeDiv(rUseful, rDemand)
    rCoverage_ = _safeDiv(rUseCount, rDemand)

    eAccuracy = _safeDiv(abs(pAccuracy - rAccuracy), rAccuracy) if rAccuracy != 0 else 0
    eCoverage = _safeDiv(abs(pCoverage - rCoverage), rCoverage) if rCoverage != 0 else 0
    eCoverage_ = _safeDiv(abs(pCoverage_ - rCoverage_), rCoverage_) if rCoverage_ != 0 else 0

    res = {
        "predict_useful": pUseful,
        "predict_useless": pUseless,
        "predict_useCount": pUseCount,
        "real_useful": rUseful,
        "real_useless": rUseless,
        "real_useCount": rUseCount,
        "demand": rDemand,
        "predict_accuracy": pAccuracy,
        "predict_coverage": pCoverage,
        "predict_coverage_2": pCoverage_,
        "real_accuracy": rAccuracy,
        "real_coverage": rCoverage,
        "real_coverage_2": rCoverage_,
        "error_accuracy": eAccuracy,
        "error_coverage": eCoverage,
        "error_coverage_2": eCoverage_,
    }
    getLogger().debug("computeMetrics: pUseful=%d rUseful=%d demand=%d", pUseful, rUseful, rDemand)
    return res


def computeAggregate(source_vals, source_weights, source_list, target_list, membership_fn, logger: logging.Logger = None):
    """通用加权聚合函数。

    参数:
        source_vals: 源元素的值字典，格式为 source_vals[src][comb_str] = {useful,...}
        source_weights: 源元素的权重字典
        source_list: 源元素列表
        target_list: 目标元素列表
        membership_fn(target, source) -> bool: 判断某 source 是否属于 target（例如 workload 属于 checkpoint）

    返回 (target_vals, target_weights)。
    target_vals[target][comb_str] 为加权聚合后的值，target_weights 为每个 target 的总权重。
    """
    target_weights = {}
    target_vals = {}
    for tgt in target_list:
        target_weights[tgt] = sum(source_weights.get(src, 0) for src in source_list if membership_fn(tgt, src))
        target_vals[tgt] = {}

    for tgt in target_list:
        tw = target_weights.get(tgt, 0)
        for src in source_list:
            if not membership_fn(tgt, src):
                continue
            sw = source_weights.get(src, 0)
            if tw == 0 or sw == 0:
                continue
            for comb_str, vals in source_vals.get(src, {}).items():
                if isinstance(vals, dict):
                    useful = vals.get("useful", 0)
                    useless = vals.get("useless", 0)
                    useCount = vals.get("useCount", 0)
                    demand = vals.get("demand", 0)
                else:
                    useful = useless = useCount = demand = 0

                agg = target_vals[tgt].setdefault(comb_str, {"useful": 0.0, "useless": 0.0, "useCount": 0, "demand": 0.0})
                agg["useful"]   += useful   * sw / tw
                agg["useless"]  += useless  * sw / tw
                agg["useCount"] += useCount * sw / tw
                agg["demand"]   += demand   * sw / tw

    return target_vals, target_weights


def computeWorkload(checkpoint_vals, checkpoint_weights, checkpoints_list, workloads_list, logger: logging.Logger = None):
    """基于 checkpoint 聚合得到 workload 层级的加权值。"""
    return computeAggregate(checkpoint_vals, checkpoint_weights, checkpoints_list, workloads_list,
                            lambda workload, checkpoint: workload in checkpoint)


def computeApp(workload_vals, workload_weights, workload_list, app_list, logger: logging.Logger = None):
    """基于 workload 聚合得到 application（benchmark）层级的加权值。"""
    return computeAggregate(workload_vals, workload_weights, workload_list, app_list,
                            lambda app, workload: app in workload)


def buildEntityDF(entity_key: str, entities: list, predict_vals: dict, real_vals: dict, logger: logging.Logger = None) -> pd.DataFrame:
    """通用 DataFrame 生成器。

    entity_key: 列名（如 'checkpoint' / 'workload' / 'app'）
    entities: 对应实体列表（如 CHECKPOINTS / WORKLOADS / APPLICATIONS）
    predict_vals: 预测值字典，格式 predict_vals[entity][comb_str] = {...}
    real_vals: 真实值字典，格式相同
    """
    if logger is None:
        logger = getLogger()

    rows = []
    for ent in entities:
        for comb in TOTAL_COMBINATIONS:
            comb_str = normalizeComb(comb)
            predictEntry = predict_vals.get(ent, {}).get(comb_str, {})
            realEntry = real_vals.get(ent, {}).get(comb_str, {})
            m = computeMetrics(predictEntry, realEntry)
            row = {entity_key: ent, "comb": comb_str}
            row.update(m)
            rows.append(row)

    df = pd.DataFrame(rows)
    desired_order = [entity_key, "comb"] + PHASE4_INT_COLS + PHASE4_PCT_COLS
    if df.empty:
        return pd.DataFrame(columns=[c for c in desired_order if c is not None])

    # numeric 列为除实体列和 comb 外的所有列
    numeric_cols = [c for c in df.columns if c not in (entity_key, "comb")]

    # 获得 comb 含有 '_' 的行作为组合预取器行
    comb_rows = df[df["comb"].str.contains("_")]
    
    # 每个 comb 的实体平均值（实体列设为 "mean"）
    agg_comb = comb_rows.groupby("comb")[numeric_cols].mean().reset_index()
    agg_comb[entity_key] = "mean"
    cols_order = [entity_key, "comb"] + numeric_cols
    df = pd.concat([df, agg_comb[cols_order]], ignore_index=True, sort=False)

    # 每个实体的 comb 平均值（comb 设为 "mean"）
    agg_ent = comb_rows.groupby(entity_key)[numeric_cols].mean().reset_index()
    agg_ent["comb"] = "mean"
    df = pd.concat([df, agg_ent[cols_order]], ignore_index=True, sort=False)

    cols = [c for c in desired_order if c in df.columns]
    return df[cols]


def buildCkptDF(checkpointPredictVals: dict, checkpointRealVals: dict, logger: logging.Logger = None) -> pd.DataFrame:
    return buildEntityDF("checkpoint", CHECKPOINTS, checkpointPredictVals, checkpointRealVals, logger)


def buildWorkloadDF(workloadPredictVals: dict, workloadRealVals: dict, logger: logging.Logger = None) -> pd.DataFrame:
    return buildEntityDF("workload", WORKLOADS, workloadPredictVals, workloadRealVals, logger)


def buildAppDF(bmkPredictVals: dict, bmkRealVals, logger: logging.Logger = None) -> pd.DataFrame:
    return buildEntityDF("app", APPLICATIONS, bmkPredictVals, bmkRealVals, logger)


def formateWrite(df: pd.DataFrame, out_path: Path, int_cols, pct_cols, logger: logging.Logger = None):
    """格式化 DataFrame 并写出 CSV。

    int_cols: 需要按整数格式化的列列表；pct_cols: 需要按百分比格式化的列列表。
    """
    dfc = df.copy()
    for col in int_cols:
        if col in dfc.columns:
            dfc[col] = dfc[col].fillna(0).round().astype(int)
    for col in pct_cols:
        if col in dfc.columns:
            dfc[col] = (dfc[col].fillna(0) * 100).round(2).map(lambda x: f"{x:.2f}%")
    dfc.to_csv(out_path, index=False)


# ---------------------- CLI 与 orchestrator ----------------------


def runRAPID(args, logger: logging.Logger = None):
    if logger is None:
        logger = getLogger()

    logger.info("Starting Phase1...")

    logger.info("Phase1: parsing PRT files")
    trace = phase1ParsePRT(max_workers=args.max_workers)
    logger.info("Phase1: completed parsing PRT files")

    logger.info("Starting Phase2...")
    phase2Result = phase2Merge(trace)
    json.dump(phase2Result, open(args.phase2_out, "w"), indent=2)
    logger.info(f"Phase2 stats write to {args.phase2_out}.")


    logger.info("Starting Phase3...")
    phase3Result = phase3Predict(trace, max_workers=args.max_workers)
    json.dump(phase3Result, open(args.phase3_out, "w"), indent=2)
    logger.info(f"Phase3 stats write to {args.phase3_out}.")

    return phase3Result
    

def runOutput(args, logger: logging.Logger = None):
    if logger is None:
        logger = getLogger()

    logger.info("Starting Phase4...")

    outDir = Path(args.phase4_out_dir)
    outDir.mkdir(parents=True, exist_ok=True)

    logger.info("Phase4: Load checkpoint predicted and real values ...")
    checkpointPredictVals = json.load(open(args.phase3_out, "r"))
    checkpointRealVals, checkpointWeights = loadCkptRealValue(CHECKPOINTS, max_workers=args.max_workers)
    ckptDF = buildCkptDF(checkpointPredictVals, checkpointRealVals)

    logger.info("Phase4: Compute Workload value ...")
    workloadPredictVals, _ = computeWorkload(checkpointPredictVals, checkpointWeights, CHECKPOINTS, WORKLOADS)
    workloadRealVals, workloadWeight = computeWorkload(checkpointRealVals, checkpointWeights, CHECKPOINTS, WORKLOADS)
    workloadDF = buildWorkloadDF(workloadPredictVals, workloadRealVals)

    logger.info("Phase4: Compute Application value ...")
    appRealVals, _ = computeApp(workloadRealVals, workloadWeight, WORKLOADS, APPLICATIONS)
    appPredictVals, _ = computeApp(workloadPredictVals, workloadWeight, WORKLOADS, APPLICATIONS)
    appDF = buildAppDF(appPredictVals, appRealVals)

    # 将重复的 CSV 写入合并为循环，使用顶部常量减少重复定义
    outputs = [
        (ckptDF, "result_phase4_checkpoint_stats.csv"),
        (workloadDF, "result_phase4_workload_stats.csv"),
        (appDF, "result_phase4_Application_stats.csv"),
    ]
    for df, name in outputs:
        formateWrite(df, outDir / name, int_cols=PHASE4_INT_COLS, pct_cols=PHASE4_PCT_COLS)
    
    logger.info("Phase4 complete.")


def main():
    args = parseArgs()
    setupLogger(args.verbose)

    runRAPID(args)
    runOutput(args)


if __name__ == "__main__":
    main()
