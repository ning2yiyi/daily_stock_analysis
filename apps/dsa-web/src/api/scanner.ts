import apiClient from './index';
import { toCamelCase } from './utils';

// ============ Types ============

export interface ScannerRunRequest {
  market?: string;
  biasThreshold?: number;
  gainMin?: number;
  gainMax?: number;
  volumeRatioMin?: number;
  scoreThreshold?: number;
  topQuant?: number;
  topFinal?: number;
}

export interface ScannerRunResponse {
  taskId: string;
}

export interface ScannerStatusResponse {
  taskId: string;
  status: 'pending' | 'scanning' | 'llm' | 'done' | 'error';
  progress: number;
  total: number;
  screened: number;
  message: string;
  error?: string;
}

export interface ScannerCandidateItem {
  code: string;
  name: string;
  market: string;
  quantScore: number;
  maScore: number;
  biasScore: number;
  volumeScore: number;
  gainScore: number;
  currentPrice?: number;
  ma5?: number;
  ma10?: number;
  ma20?: number;
  biasMa5?: number;
  volumeRatio?: number;
  gain20d?: number;
  peRatio?: number;
  pbRatio?: number;
  llmRank?: number;
  llmReason?: string;
  llmSelected: boolean;
  confirmed: boolean;
}

export interface ScannerCandidatesResponse {
  taskId?: string;
  candidates: ScannerCandidateItem[];
  total: number;
}

export interface ScannerConfirmResponse {
  confirmed: number;
  added: string[];
  stockList: string[];
}

// ============ API ============

export const scannerApi = {
  run: async (params: ScannerRunRequest = {}): Promise<ScannerRunResponse> => {
    const requestData: Record<string, unknown> = {};
    if (params.market) requestData.market = params.market;
    if (params.biasThreshold != null) requestData.bias_threshold = params.biasThreshold;
    if (params.gainMin != null) requestData.gain_min = params.gainMin;
    if (params.gainMax != null) requestData.gain_max = params.gainMax;
    if (params.volumeRatioMin != null) requestData.volume_ratio_min = params.volumeRatioMin;
    if (params.scoreThreshold != null) requestData.score_threshold = params.scoreThreshold;
    if (params.topQuant != null) requestData.top_quant = params.topQuant;
    if (params.topFinal != null) requestData.top_final = params.topFinal;

    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/scanner/run',
      requestData,
    );
    return toCamelCase<ScannerRunResponse>(response.data);
  },

  getStatus: async (taskId: string): Promise<ScannerStatusResponse> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/scanner/status/${taskId}`,
    );
    return toCamelCase<ScannerStatusResponse>(response.data);
  },

  getCandidates: async (params: {
    taskId?: string;
    llmOnly?: boolean;
  } = {}): Promise<ScannerCandidatesResponse> => {
    const queryParams: Record<string, string | boolean> = {};
    if (params.taskId) queryParams.task_id = params.taskId;
    if (params.llmOnly != null) queryParams.llm_only = params.llmOnly;

    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/scanner/candidates',
      { params: queryParams },
    );
    return toCamelCase<ScannerCandidatesResponse>(response.data);
  },

  confirm: async (taskId: string, codes: string[]): Promise<ScannerConfirmResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/scanner/confirm',
      { task_id: taskId, codes },
    );
    return toCamelCase<ScannerConfirmResponse>(response.data);
  },
};
