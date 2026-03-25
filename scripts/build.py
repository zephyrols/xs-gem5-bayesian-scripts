import config
import os
import shutil
import subprocess
import sys
import argparse
from pathlib import Path


def setup_environment_variables(env_config):
    """
    根据配置设置环境变量
    
    Args:
        env_config: 环境配置对象
        
    Returns:
        dict: 包含环境变量的字典
    """
    env_vars = os.environ.copy()
    
    # 设置 restorer 相关环境变量
    if env_config.restorer['type']:
        env_vars[env_config.restorer['type']] = env_config.restorer['path']
        print(f"Set environment variable: {env_config.restorer['type']} = {env_config.restorer['path']}")
    
    # 设置 ref_so 相关环境变量  
    if env_config.ref_so['type']:
        env_vars[env_config.ref_so['type']] = env_config.ref_so['path']
        print(f"Set environment variable: {env_config.ref_so['type']} = {env_config.ref_so['path']}")
    
    return env_vars


def run_command_with_env(command, env_vars, cwd=None, shell=True):
    """
    使用指定环境变量执行shell命令
    
    Args:
        command: 要执行的命令
        env_vars: 环境变量字典
        cwd: 工作目录
        shell: 是否使用shell执行
        
    Returns:
        tuple: (returncode, stdout, stderr)
    """
    print(f"Executing: {command}")
    if cwd:
        print(f"Working directory: {cwd}")
    
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            env=env_vars,
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.stdout:
            print("STDOUT:")
            print(result.stdout)
        
        if result.stderr:
            print("STDERR:")
            print(result.stderr)
            
        return result.returncode, result.stdout, result.stderr
        
    except Exception as e:
        print(f"Error executing command: {e}")
        return -1, "", str(e)


def run_command(command, cwd=None, shell=True):
    """
    执行shell命令并返回结果
    
    Args:
        command: 要执行的命令
        cwd: 工作目录
        shell: 是否使用shell执行
        
    Returns:
        tuple: (returncode, stdout, stderr)
    """
    print(f"Executing: {command}")
    if cwd:
        print(f"Working directory: {cwd}")
    
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.stdout:
            print("STDOUT:")
            print(result.stdout)
        
        if result.stderr:
            print("STDERR:")
            print(result.stderr)
            
        return result.returncode, result.stdout, result.stderr
        
    except Exception as e:
        print(f"Error executing command: {e}")
        return -1, "", str(e)


def build_gem5(config_file, debug=False, build_threads=None):
    """
    构建gem5并复制到指定位置
    
    Args:
        config_file: 配置文件路径
        debug: 是否编译debug版本
        build_threads: 编译线程数
    """
    try:
        # 加载配置
        print("Loading configuration...")
        configs = config.load_yaml_new(config_file, ['env', 'run'])
        env_config = configs['env']
        run_config = configs['run']
        
        print(f"GEM5 Home: {env_config.gem5_home}")
        print(f"Bin Home: {env_config.bin_home}")
        print(f"Target Binary: {run_config.gem5_bin}")
        
        # 检查gem5_home是否存在
        gem5_home = Path(env_config.gem5_home)
        if not gem5_home.exists():
            print(f"Error: GEM5 home directory does not exist: {gem5_home}")
            return False
            
        # 检查PGO脚本是否存在
        pgo_script = gem5_home / "util/pgo/basic_pgo_new.sh"
        if not pgo_script.exists():
            print(f"Error: PGO script does not exist: {pgo_script}")
            return False
            
        # 设置环境变量
        print("Setting up environment variables...")
        env_vars = setup_environment_variables(env_config)
        
        # 确定编译线程数
        if build_threads is None:
            build_threads = os.cpu_count() or 4
        
        print(f"Starting GEM5 build (debug={debug}, threads={build_threads})...")
        
        # 根据debug参数选择编译命令和目标文件
        if debug:
            build_command = f"scons build/RISCV/gem5.debug -j {build_threads} --gold-linker"
            source_binary = gem5_home / "build/RISCV/gem5.debug"
        else:
            # 执行PGO构建脚本（使用环境变量）
            build_command = "util/pgo/basic_pgo_new.sh"
            source_binary = gem5_home / "build/RISCV/gem5.fast"
        
        # 执行构建命令
        returncode, stdout, stderr = run_command_with_env(
            build_command,
            env_vars,
            cwd=str(gem5_home)
        )
        
        if returncode != 0:
            build_type = "debug" if debug else "PGO"
            print(f"Error: {build_type} build failed with return code {returncode}")
            return False
            
        build_type = "debug" if debug else "PGO"
        print(f"{build_type} build completed successfully!")
        
        # 检查构建的二进制文件是否存在
        if not source_binary.exists():
            print(f"Error: Built binary does not exist: {source_binary}")
            return False
            
        # 确保目标目录存在
        bin_home = Path(env_config.bin_home)
        bin_home.mkdir(parents=True, exist_ok=True)
        
        # 获取目标文件名（从gem5_bin配置中提取）
        target_binary_name = Path(run_config.gem5_bin).name
        target_binary = bin_home / target_binary_name
        
        print(f"Copying {source_binary} to {target_binary}")
        
        # 复制文件
        try:
            shutil.copy2(source_binary, target_binary)
            print(f"Successfully copied binary to: {target_binary}")
            
            # 设置执行权限
            os.chmod(target_binary, 0o755)
            print("Set executable permissions")
            
        except Exception as e:
            print(f"Error copying binary: {e}")
            return False
            
        print("Build and deployment completed successfully!")
        return True
        
    except Exception as e:
        print(f"Error in build process: {e}")
        return False


def main():
    """
    主函数
    """
    parser = argparse.ArgumentParser(
        description="Build GEM5 using PGO and deploy to bin directory"
    )
    parser.add_argument(
        "config_file",
        type=str,
        help="Path to the YAML configuration file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually executing"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Build debug version instead of PGO optimized version"
    )
    parser.add_argument(
        "--build-threads",
        type=int,
        help="Number of threads to use for building (default: number of CPU cores)"
    )
    
    args = parser.parse_args()
    
    # 检查配置文件是否存在
    config_path = Path(args.config_file)
    if not config_path.exists():
        print(f"Error: Configuration file does not exist: {config_path}")
        sys.exit(1)
        
    if args.dry_run:
        print("DRY RUN MODE - showing configuration only")
        config.print_config(args.config_file)
        build_type = "debug" if args.debug else "PGO optimized"
        threads = args.build_threads or os.cpu_count() or 4
        print(f"\nBuild configuration:")
        print(f"  Build type: {build_type}")
        print(f"  Build threads: {threads}")
        return
        
    # 执行构建
    success = build_gem5(args.config_file, debug=args.debug, build_threads=args.build_threads)
    
    if success:
        print("\n✓ Build completed successfully!")
        sys.exit(0)
    else:
        print("\n✗ Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()