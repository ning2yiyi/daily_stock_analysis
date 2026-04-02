import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { systemConfigApi } from '../../api/systemConfig';
import { SystemConfigConflictError } from '../../api/systemConfig';
import { useStockIndex } from '../../hooks/useStockIndex';
import { Card } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { cn } from '../../utils/cn';

interface WatchlistPanelProps {
  /** 点击自选股时填充输入框（仅选中，不发起分析） */
  onSelect: (code: string) => void;
  /** 当前正在分析中的股票代码 */
  analyzingCode?: string;
  /** 当前查看报告的股票代码（高亮显示） */
  activeCode?: string;
  className?: string;
}

/**
 * 将 STOCK_LIST 中的代码标准化为 stocks.index.json 里 displayCode 的格式。
 * HK 股票在 index 中 displayCode 是 "00700"，但 STOCK_LIST 里可能是 "hk00700"。
 */
function normalizeCodeForLookup(code: string): string {
  const upper = code.toUpperCase();
  if (/^HK\d+$/.test(upper)) return upper.slice(2); // HK00700 → 00700
  return upper;
}

/**
 * 自选股面板
 *
 * 从系统配置读取 STOCK_LIST，展示为可点击的快捷分析入口。
 * 通过复用 useStockIndex 获取股票名称（stocks.index.json 已由 StockAutocomplete 缓存）。
 */
export const WatchlistPanel: React.FC<WatchlistPanelProps> = ({
  onSelect,
  analyzingCode,
  activeCode,
  className,
}) => {
  const [codes, setCodes] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [deletingCode, setDeletingCode] = useState<string | null>(null);
  const configVersionRef = useRef<string>('');
  const maskTokenRef = useRef<string>('');

  // 复用股票索引，获取 displayCode → nameZh 映射（与 StockAutocomplete 共用同一份缓存）
  const { index } = useStockIndex();
  const nameMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of index) {
      map.set(item.displayCode.toUpperCase(), item.nameZh);
    }
    return map;
  }, [index]);

  const load = useCallback(async () => {
    setIsLoading(true);
    setHasError(false);
    try {
      const res = await systemConfigApi.getConfig(false);
      configVersionRef.current = res.configVersion;
      maskTokenRef.current = res.maskToken;
      const raw = res.items.find((item) => item.key === 'STOCK_LIST')?.value ?? '';
      setCodes(raw.split(',').map((s) => s.trim()).filter(Boolean));
    } catch {
      setHasError(true);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleDelete = useCallback(async (codeToDelete: string) => {
    setDeletingCode(codeToDelete);
    try {
      const newCodes = codes.filter((c) => c.toUpperCase() !== codeToDelete.toUpperCase());
      const value = newCodes.join(',');
      const res = await systemConfigApi.update({
        configVersion: configVersionRef.current,
        maskToken: maskTokenRef.current,
        reloadNow: true,
        items: [{ key: 'STOCK_LIST', value }],
      });
      configVersionRef.current = res.configVersion;
      setCodes(newCodes);
    } catch (e) {
      if (e instanceof SystemConfigConflictError) {
        // 版本冲突时重新加载
        await load();
      }
    } finally {
      setDeletingCode(null);
    }
  }, [codes, load]);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshButton = (
    <button
      type="button"
      onClick={() => void load()}
      className="text-muted-text transition-colors hover:text-foreground"
      aria-label="刷新自选股列表"
    >
      <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
          d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
      </svg>
    </button>
  );

  return (
    <Card
      variant="bordered"
      padding="none"
      className={cn('home-panel-card overflow-hidden', className)}
    >
      <div className="border-b border-subtle px-3 py-3">
        <DashboardPanelHeader
          className="mb-0"
          eyebrow="自选股"
          accentEyebrow
          headingClassName="items-center"
          actions={refreshButton}
        />
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center px-3 py-4">
          <div className="home-spinner h-4 w-4 animate-spin border-2" aria-hidden="true" />
        </div>
      ) : hasError ? (
        <p className="px-3 py-4 text-center text-xs text-muted-text">加载失败，请点击刷新重试</p>
      ) : codes.length === 0 ? (
        <p className="px-3 py-4 text-center text-xs text-muted-text">
          自选股为空，请先在设置中配置 STOCK_LIST
        </p>
      ) : (
        <div className="max-h-52 overflow-y-auto custom-scrollbar">
          <div className="flex flex-col gap-0.5 p-2">
            {codes.map((code) => {
              const isActive = activeCode?.toUpperCase() === code.toUpperCase();
              const isBusy = analyzingCode?.toUpperCase() === code.toUpperCase();
              const isDeleting = deletingCode?.toUpperCase() === code.toUpperCase();
              const nameZh = nameMap.get(normalizeCodeForLookup(code));
              return (
                <button
                  key={code}
                  type="button"
                  disabled={isBusy || isDeleting}
                  onClick={() => onSelect(code)}
                  className={cn(
                    'group flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors',
                    isActive ? 'bg-primary/10 text-primary' : 'text-foreground hover:bg-hover',
                    (isBusy || isDeleting) && 'cursor-wait opacity-60',
                  )}
                  aria-label={`分析 ${nameZh ?? code}`}
                  aria-current={isActive ? 'true' : undefined}
                >
                  {/* 股票代码 */}
                  <span className="w-16 flex-shrink-0 font-mono text-xs font-medium tracking-wide">
                    {code}
                  </span>
                  {/* 股票名称 */}
                  <span className={cn(
                    'flex-1 truncate text-xs',
                    isActive ? 'text-primary/80' : 'text-muted-text',
                  )}>
                    {nameZh ?? '—'}
                  </span>
                  {/* 右侧：正常显示箭头，hover 显示删除按钮 */}
                  {isBusy ? (
                    <div
                      className="home-spinner h-3 w-3 flex-shrink-0 animate-spin border border-current"
                      aria-hidden="true"
                    />
                  ) : isDeleting ? (
                    <div
                      className="home-spinner h-3 w-3 flex-shrink-0 animate-spin border border-current"
                      aria-hidden="true"
                    />
                  ) : (
                    <>
                      {/* 箭头图标 - hover 时隐藏 */}
                      <svg
                        className="h-3.5 w-3.5 flex-shrink-0 text-muted-text opacity-0 transition-opacity group-hover:hidden"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        aria-hidden="true"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                      {/* 删除图标 - hover 时显示 */}
                      <span
                        role="button"
                        tabIndex={-1}
                        onClick={(e) => {
                          e.stopPropagation();
                          void handleDelete(code);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.stopPropagation();
                            e.preventDefault();
                            void handleDelete(code);
                          }
                        }}
                        className="hidden h-3.5 w-3.5 flex-shrink-0 items-center justify-center rounded text-muted-text transition-colors hover:text-red-500 group-hover:flex"
                        aria-label={`删除自选股 ${nameZh ?? code}`}
                      >
                        <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </span>
                    </>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </Card>
  );
};
