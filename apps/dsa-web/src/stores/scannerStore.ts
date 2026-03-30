import { create } from 'zustand';
import {
  scannerApi,
  type ScannerCandidateItem,
  type ScannerRunRequest,
  type ScannerStatusResponse,
} from '../api/scanner';

interface ScannerState {
  // Task state
  taskId: string | null;
  status: ScannerStatusResponse | null;
  polling: boolean;

  // Results
  candidates: ScannerCandidateItem[];
  selectedCodes: Set<string>;

  // UI state
  running: boolean;
  confirming: boolean;
  error: string | null;

  // Actions
  startScan: (params?: ScannerRunRequest) => Promise<void>;
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
  taskId: null,
  status: null,
  polling: false,
  candidates: [],
  selectedCodes: new Set(),
  running: false,
  confirming: false,
  error: null,

  startScan: async (params?: ScannerRunRequest) => {
    set({ running: true, error: null, candidates: [], selectedCodes: new Set(), status: null });
    try {
      const res = await scannerApi.run(params);
      set({ taskId: res.taskId });
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
    const { taskId } = get();
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
      set({ candidates: res.candidates, taskId: res.taskId ?? taskId ?? null });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      set({ error: msg });
    }
  },

  toggleSelect: (code: string) => {
    set((state) => {
      const next = new Set(state.selectedCodes);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return { selectedCodes: next };
    });
  },

  selectAll: () => {
    set((state) => ({
      selectedCodes: new Set(state.candidates.map((c) => c.code)),
    }));
  },

  deselectAll: () => {
    set({ selectedCodes: new Set() });
  },

  confirmSelected: async () => {
    const { taskId, selectedCodes } = get();
    if (!taskId || selectedCodes.size === 0) return null;
    set({ confirming: true, error: null });
    try {
      const res = await scannerApi.confirm(taskId, Array.from(selectedCodes));
      // Mark confirmed in local state
      set((state) => ({
        candidates: state.candidates.map((c) =>
          selectedCodes.has(c.code) ? { ...c, confirmed: true } : c,
        ),
        confirming: false,
        selectedCodes: new Set(),
      }));
      return { added: res.added, stockList: res.stockList };
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      set({ confirming: false, error: msg });
      return null;
    }
  },

  reset: () => {
    get().stopPolling();
    set({
      taskId: null,
      status: null,
      polling: false,
      candidates: [],
      selectedCodes: new Set(),
      running: false,
      confirming: false,
      error: null,
    });
  },
}));
