import { create } from 'zustand';
import {
  scannerApi,
  type ScannerCandidateItem,
  type ScannerRunRequest,
  type ScannerStatusResponse,
} from '../api/scanner';

/** Per-market scan data kept independently so switching tabs preserves results. */
export interface MarketScanData {
  taskId: string | null;
  candidates: ScannerCandidateItem[];
  selectedCodes: Set<string>;
}

function emptyMarketData(): MarketScanData {
  return { taskId: null, candidates: [], selectedCodes: new Set() };
}

interface ScannerState {
  /** Currently viewed market. */
  market: 'us' | 'cn';
  /** Scan results stored per market so switching tabs shows the correct list. */
  marketData: Record<string, MarketScanData>;

  // Shared UI state (only one scan can run at a time)
  status: ScannerStatusResponse | null;
  polling: boolean;
  running: boolean;
  confirming: boolean;
  error: string | null;

  // Actions
  setMarket: (m: 'us' | 'cn') => void;
  startScan: (params?: Omit<ScannerRunRequest, 'market'>) => Promise<void>;
  pollStatus: () => Promise<void>;
  stopPolling: () => void;
  loadCandidates: (taskId?: string) => Promise<void>;
  toggleSelect: (code: string) => void;
  selectAll: () => void;
  deselectAll: () => void;
  confirmSelected: () => Promise<{ added: string[]; stockList: string[] } | null>;
  reset: () => void;
}

let pollTimer: ReturnType<typeof setInterval> | null = null;

export const useScannerStore = create<ScannerState>((set, get) => ({
  market: 'us',
  marketData: {
    us: emptyMarketData(),
    cn: emptyMarketData(),
  },
  status: null,
  polling: false,
  running: false,
  confirming: false,
  error: null,

  setMarket: (m) => {
    set({ market: m, error: null });
  },

  startScan: async (params) => {
    const { market } = get();
    // Clear current market's data and start scan
    set((state) => ({
      running: true,
      error: null,
      status: null,
      marketData: {
        ...state.marketData,
        [market]: emptyMarketData(),
      },
    }));
    try {
      const res = await scannerApi.run({ ...params, market });
      set((state) => ({
        marketData: {
          ...state.marketData,
          [market]: { ...state.marketData[market], taskId: res.taskId },
        },
      }));
      // Start polling
      get().pollStatus();
      set({ polling: true });
      pollTimer = setInterval(() => {
        get().pollStatus();
      }, 2000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      set({ running: false, error: msg });
    }
  },

  pollStatus: async () => {
    const { market, marketData } = get();
    const taskId = marketData[market]?.taskId;
    if (!taskId) return;
    try {
      const status = await scannerApi.getStatus(taskId);
      set({ status });
      if (status.status === 'done' || status.status === 'error') {
        get().stopPolling();
        set({ running: false });
        if (status.status === 'done') {
          await get().loadCandidates(taskId);
        }
        if (status.status === 'error') {
          set({ error: status.error || status.message });
        }
      }
    } catch {
      // Ignore transient polling errors
    }
  },

  stopPolling: () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    set({ polling: false });
  },

  loadCandidates: async (taskId?: string) => {
    try {
      const res = await scannerApi.getCandidates({ taskId, llmOnly: true });
      // Determine which market these candidates belong to
      const targetMarket = res.candidates.length > 0
        ? res.candidates[0].market
        : get().market;
      const resolvedTaskId = res.taskId ?? taskId ?? null;
      set((state) => ({
        marketData: {
          ...state.marketData,
          [targetMarket]: {
            taskId: resolvedTaskId,
            candidates: res.candidates,
            selectedCodes: new Set(),
          },
        },
      }));
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      set({ error: msg });
    }
  },

  toggleSelect: (code: string) => {
    const { market } = get();
    set((state) => {
      const current = state.marketData[market];
      const next = new Set(current.selectedCodes);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return {
        marketData: {
          ...state.marketData,
          [market]: { ...current, selectedCodes: next },
        },
      };
    });
  },

  selectAll: () => {
    const { market } = get();
    set((state) => {
      const current = state.marketData[market];
      return {
        marketData: {
          ...state.marketData,
          [market]: {
            ...current,
            selectedCodes: new Set(current.candidates.map((c) => c.code)),
          },
        },
      };
    });
  },

  deselectAll: () => {
    const { market } = get();
    set((state) => {
      const current = state.marketData[market];
      return {
        marketData: {
          ...state.marketData,
          [market]: { ...current, selectedCodes: new Set() },
        },
      };
    });
  },

  confirmSelected: async () => {
    const { market, marketData } = get();
    const { taskId, selectedCodes } = marketData[market];
    if (!taskId || selectedCodes.size === 0) return null;
    set({ confirming: true, error: null });
    try {
      const res = await scannerApi.confirm(taskId, Array.from(selectedCodes));
      // Mark confirmed in local state
      set((state) => {
        const current = state.marketData[market];
        return {
          confirming: false,
          marketData: {
            ...state.marketData,
            [market]: {
              ...current,
              candidates: current.candidates.map((c) =>
                selectedCodes.has(c.code) ? { ...c, confirmed: true } : c,
              ),
              selectedCodes: new Set(),
            },
          },
        };
      });
      return { added: res.added, stockList: res.stockList };
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      set({ confirming: false, error: msg });
      return null;
    }
  },

  reset: () => {
    get().stopPolling();
    const { market } = get();
    set((state) => ({
      status: null,
      polling: false,
      running: false,
      confirming: false,
      error: null,
      marketData: {
        ...state.marketData,
        [market]: emptyMarketData(),
      },
    }));
  },
}));
