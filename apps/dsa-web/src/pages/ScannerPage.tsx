import type React from 'react';
import { useEffect, useCallback, useState } from 'react';
import { useScannerStore } from '../stores/scannerStore';
import { Card, Badge, AppPage, PageHeader } from '../components/common';

function scoreBadge(score: number) {
  if (score >= 80) return <Badge variant="success" glow>{score}</Badge>;
  if (score >= 60) return <Badge variant="success">{score}</Badge>;
  if (score >= 40) return <Badge variant="warning">{score}</Badge>;
  return <Badge variant="default">{score}</Badge>;
}

function pct(v?: number | null): string {
  if (v == null) return '--';
  return `${v.toFixed(1)}%`;
}

function num(v?: number | null, d = 2): string {
  if (v == null) return '--';
  return v.toFixed(d);
}

const ScannerPage: React.FC = () => {
  const {
    taskId,
    status,
    running,
    confirming,
    candidates,
    selectedCodes,
    error,
    startScan,
    loadCandidates,
    toggleSelect,
    selectAll,
    deselectAll,
    confirmSelected,
    reset,
  } = useScannerStore();

  const [confirmResult, setConfirmResult] = useState<{ added: string[]; stockList: string[] } | null>(null);

  // Load latest candidates on mount
  useEffect(() => {
    loadCandidates();
    return () => {
      useScannerStore.getState().stopPolling();
    };
  }, [loadCandidates]);

  const handleStart = useCallback(() => {
    setConfirmResult(null);
    startScan({ market: 'us' });
  }, [startScan]);

  const handleConfirm = useCallback(async () => {
    const result = await confirmSelected();
    if (result) {
      setConfirmResult(result);
    }
  }, [confirmSelected]);

  const progress = status
    ? status.total > 0
      ? Math.round((status.progress / status.total) * 100)
      : 0
    : 0;

  return (
    <AppPage>
      <PageHeader
        title="选股扫描"
        description="量化初筛 + LLM 精选，发现值得关注的美股标的"
      />

      {/* Controls */}
      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-primary"
            onClick={handleStart}
            disabled={running}
          >
            {running ? '扫描中...' : '开始扫描 (美股)'}
          </button>
          {taskId && !running && candidates.length > 0 && (
            <>
              <button
                type="button"
                className="btn-secondary"
                onClick={selectAll}
              >
                全选
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={deselectAll}
              >
                取消全选
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={handleConfirm}
                disabled={confirming || selectedCodes.size === 0}
              >
                {confirming ? '确认中...' : `加入自选 (${selectedCodes.size})`}
              </button>
              <button
                type="button"
                className="btn-ghost text-sm"
                onClick={reset}
              >
                清空
              </button>
            </>
          )}
        </div>

        {/* Progress bar */}
        {running && status && (
          <div className="mt-3">
            <div className="mb-1 flex items-center justify-between text-xs text-secondary-text">
              <span>{status.message}</span>
              <span>{progress}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-border/40">
              <div
                className="h-full rounded-full bg-primary-gradient transition-all duration-500"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="mt-3 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}

        {/* Confirm result */}
        {confirmResult && (
          <div className="mt-3 rounded-lg border border-success/30 bg-success/5 px-3 py-2 text-sm text-success">
            已加入自选: {confirmResult.added.length > 0 ? confirmResult.added.join(', ') : '(无新增，已在自选中)'}
          </div>
        )}
      </Card>

      {/* Results Table */}
      {candidates.length > 0 && (
        <Card className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border/50 bg-card-hover/50 text-left text-xs text-secondary-text">
                <th className="w-10 px-3 py-2.5">
                  <input
                    type="checkbox"
                    checked={selectedCodes.size === candidates.length && candidates.length > 0}
                    onChange={() =>
                      selectedCodes.size === candidates.length
                        ? deselectAll()
                        : selectAll()
                    }
                  />
                </th>
                <th className="px-3 py-2.5">排名</th>
                <th className="px-3 py-2.5">代码</th>
                <th className="px-3 py-2.5">名称</th>
                <th className="px-3 py-2.5 text-right">总分</th>
                <th className="px-3 py-2.5 text-right">MA</th>
                <th className="px-3 py-2.5 text-right">乖离率</th>
                <th className="px-3 py-2.5 text-right">量比</th>
                <th className="px-3 py-2.5 text-right">涨幅</th>
                <th className="px-3 py-2.5 text-right">价格</th>
                <th className="px-3 py-2.5 text-right">MA5</th>
                <th className="px-3 py-2.5 text-right">MA10</th>
                <th className="px-3 py-2.5 text-right">MA20</th>
                <th className="px-3 py-2.5 text-right">PE</th>
                <th className="px-3 py-2.5 text-right">PB</th>
                <th className="min-w-[200px] px-3 py-2.5">LLM 理由</th>
                <th className="px-3 py-2.5">状态</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => (
                <tr
                  key={c.code}
                  className="border-b border-border/30 transition-colors hover:bg-card-hover/30"
                >
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selectedCodes.has(c.code)}
                      onChange={() => toggleSelect(c.code)}
                      disabled={c.confirmed}
                    />
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {c.llmRank ?? '--'}
                  </td>
                  <td className="px-3 py-2 font-mono font-medium">
                    {c.code}
                  </td>
                  <td className="px-3 py-2">
                    {c.name}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {scoreBadge(Math.round(c.quantScore))}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {c.maScore}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {pct(c.biasMa5)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {num(c.volumeRatio, 1)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {pct(c.gain20d)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono">
                    ${num(c.currentPrice)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {num(c.ma5)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {num(c.ma10)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {num(c.ma20)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {num(c.peRatio, 1)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-xs">
                    {num(c.pbRatio, 1)}
                  </td>
                  <td className="px-3 py-2 text-xs text-secondary-text">
                    {c.llmReason || '--'}
                  </td>
                  <td className="px-3 py-2">
                    {c.confirmed ? (
                      <Badge variant="success">已加入</Badge>
                    ) : c.llmSelected ? (
                      <Badge variant="info">推荐</Badge>
                    ) : (
                      <Badge variant="default">候选</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {/* Empty state */}
      {!running && candidates.length === 0 && !error && (
        <Card className="py-16 text-center">
          <p className="text-secondary-text">
            点击「开始扫描」，从标普500 + 纳斯达克100中筛选值得关注的标的
          </p>
          <p className="mt-2 text-xs text-muted-text">
            量化初筛（MA多头/乖离率/量比/涨幅）→ LLM精选 Top 10 → 确认加入自选
          </p>
        </Card>
      )}
    </AppPage>
  );
};

export default ScannerPage;
