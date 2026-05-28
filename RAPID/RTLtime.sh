#!/bin/bash
# filepath: extract_host_time_simple.sh

echo "扫描所有 simulator_out.txt 文件..."
echo "=================================="

total_time_ms=0
temp_file=$(mktemp)
# 根据实际路径调整以下命令中的路径前缀
find /nfs/home/ci-runner/master-perf-report/cr250801-99d41d3 -name "simulator_out.txt" -exec grep -H "Host time spent:" {} \; | while read -r line; do
    file=$(echo "$line" | cut -d: -f1-3)  # 处理路径中可能包含冒号的情况
    time_ms=$(echo "$line" | grep -oE '[0-9]+ms' | tr -d 'ms')

    if [[ -n "$time_ms" ]]; then
        echo "$time_ms" >> "$temp_file"
        time_s=$(echo "scale=2; $time_ms / 1000" | bc 2>/dev/null || echo "0")
        printf "%-60s: %10s ms (%8s s)\n" "$file" "$time_ms" "$time_s"
    fi
done

# 计算总时间
if [[ -s "$temp_file" ]]; then
    total_time_ms=$(awk '{sum += $1} END {print sum}' "$temp_file")
    total_time_s=$(echo "scale=2; $total_time_ms / 1000" | bc 2>/dev/null || echo "0")
    total_time_min=$(echo "scale=2; $total_time_s / 60" | bc 2>/dev/null || echo "0")
    
    echo "=================================="
    echo "总计时间统计:"
    printf "总时间: %s ms (%s s) (%s min)\n" "$total_time_ms" "$total_time_s" "$total_time_min"
    
    # 统计文件数量
    file_count=$(wc -l < "$temp_file")
    echo "处理文件数量: $file_count"
    
    if [[ $file_count -gt 0 ]]; then
        avg_time_ms=$(echo "scale=2; $total_time_ms / $file_count" | bc 2>/dev/null || echo "0")
        avg_time_s=$(echo "scale=2; $avg_time_ms / 1000" | bc 2>/dev/null || echo "0")
        printf "平均时间: %s ms (%s s)\n" "$avg_time_ms" "$avg_time_s"
    fi
else
    echo "未找到任何有效的时间数据"
fi

# 清理临时文件
rm -f "$temp_file"