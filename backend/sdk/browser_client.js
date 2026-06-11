export class LCUClient {
  constructor(baseUrl = '') {
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  async request(path, options = {}) {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    return response.json();
  }

  getStatus() {
    return this.request('/api/status');
  }

  listProviderPresets() {
    return this.request('/api/llm/providers');
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
}
