// Copyright 2026 LCU Mod Contributors
// SPDX-License-Identifier: Apache-2.0

export class LCUClient {
  constructor(baseUrl = '', { apiToken = '' } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.apiToken = apiToken;
  }

  async request(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body) headers['Content-Type'] = 'application/json';
    if (this.apiToken) headers.Authorization = `Bearer ${this.apiToken}`;
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...options,
      headers,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || `LCU request failed with HTTP ${response.status}`);
    }
    return data;
  }

  getStatus() {
    return this.request('/api/status');
  }

  getSDKInfo() {
    return this.request('/api/sdk/info');
  }

  getV2Info() {
    return this.request('/api/v2/info');
  }

  getSession() {
    return this.request('/api/session');
  }

  getMemory() {
    return this.request('/api/memory');
  }

  async getIdentity() {
    return (await this.request('/api/sdk/identity')).identity || {};
  }

  setIdentity(identity = {}) {
    return this.request('/api/sdk/identity', {
      method: 'POST',
      body: JSON.stringify(identity),
    });
  }

  listProviderPresets() {
    return this.request('/api/llm/providers');
  }

  async getProviderPresets() {
    return (await this.listProviderPresets()).providers || [];
  }

  setLLMConfig(agent = 'default', config = {}) {
    return this.request('/api/llm/config', {
      method: 'POST',
      body: JSON.stringify({ agent, ...config }),
    });
  }

  fetchModels(agent = 'default', overrides = {}) {
    return this.request('/api/llm/models', {
      method: 'POST',
      body: JSON.stringify({ agent, ...overrides }),
    });
  }

  async getModels(agent = 'default', overrides = {}) {
    const result = await this.fetchModels(agent, overrides);
    return result.models || [];
  }

  getPersona() {
    return this.request('/api/persona');
  }

  setPersona(persona = {}) {
    return this.request('/api/persona', {
      method: 'POST',
      body: JSON.stringify(persona),
    });
  }

  pushExternalContext(external_context = {}) {
    return this.request('/api/sdk/context', {
      method: 'POST',
      body: JSON.stringify({ external_context }),
    });
  }

  getExternalContext() {
    return this.request('/api/sdk/context');
  }

  async sendChat(message, sender = 'sdk') {
    const result = await this.request('/api/sdk/chat', {
      method: 'POST',
      body: JSON.stringify({ message, sender }),
    });
    return result.response || '';
  }

  async sendCommand(command, args = {}) {
    const result = await this.request('/api/sdk/command', {
      method: 'POST',
      body: JSON.stringify({ command, args }),
    });
    return result.request_id;
  }

  async listSkills(category = '') {
    const suffix = category ? `?category=${encodeURIComponent(category)}` : '';
    const result = await this.request(`/api/v2/skills${suffix}`);
    return result.skills || [];
  }

  getSkill(skillId) {
    return this.request(`/api/v2/skills/${encodeURIComponent(skillId)}`);
  }

  getControl() {
    return this.request('/api/v2/control');
  }

  async acquireControl(owner, { mode = 'external', owns, ttlSeconds = 30 } = {}) {
    const payload = { owner, mode, ttl_seconds: ttlSeconds };
    if (owns) payload.owns = owns;
    const result = await this.request('/api/v2/control/leases', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return result.lease;
  }

  async heartbeatControl(leaseId, fencingToken, ttlSeconds = 30) {
    const result = await this.request(`/api/v2/control/leases/${leaseId}/heartbeat`, {
      method: 'POST',
      body: JSON.stringify({ fencing_token: fencingToken, ttl_seconds: ttlSeconds }),
    });
    return result.lease;
  }

  async releaseControl(leaseId, fencingToken) {
    const result = await this.request(`/api/v2/control/leases/${leaseId}/release`, {
      method: 'POST',
      body: JSON.stringify({ fencing_token: fencingToken }),
    });
    return result.lease;
  }

  runSkill(skillId, input = {}, { leaseId, fencingToken } = {}) {
    const payload = { input };
    if (leaseId) payload.lease_id = leaseId;
    if (fencingToken) payload.fencing_token = fencingToken;
    return this.request(`/api/v2/skills/${encodeURIComponent(skillId)}/runs`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  getRuntimeConfig() {
    return this.request('/api/config');
  }

  updateRuntimeConfig(config = {}) {
    return this.request('/api/config', {
      method: 'POST',
      body: JSON.stringify(config),
    });
  }
}
