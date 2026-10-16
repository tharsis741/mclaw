import { useState } from 'react';
import { Plug } from 'lucide-react';

const LOGOS: Record<string, string> = {
  qwen: 'qwen',
  'qwen-intl': 'qwen',
  deepseek: 'deepseek',
  moonshot: 'kimi',
  'moonshot-intl': 'kimi',
  minimax: 'minimax',
  'minimax-cn': 'minimax',
  zhipu: 'zhipu',
  doubao: 'doubao',
  baidu: 'baidu',
  tencent: 'hunyuan',
  baichuan: 'baichuan',
  xiaomi: 'xiaomimimo',
  openai: 'openai',
  anthropic: 'anthropic',
  google: 'gemini',
  xai: 'xai',
  meta: 'meta',
  mistral: 'mistral',
  groq: 'groq',
  together: 'together',
  fireworks: 'fireworks',
  deepinfra: 'deepinfra',
  openrouter: 'openrouter',
  stepfun: 'stepfun',
  yi: 'yi',
  cohere: 'cohere',
  perplexity: 'perplexity',
  microsoft: 'microsoft',
  amazon: 'bedrock',
  siliconflow: 'siliconcloud',
};

export function ProviderLogo({ provider }: { provider: string }) {
  const [failedLogo, setFailedLogo] = useState<string | null>(null);
  const name = LOGOS[provider];
  const available = name && name !== failedLogo;
  return (
    <span
      className={`provider-logo${available && name === 'kimi' ? ' provider-logo--dark' : ''}`}
      aria-hidden="true"
    >
      {available ? (
        <img
          key={name}
          src={`/provider-logos/${name}.svg`}
          width={28}
          height={28}
          alt=""
          onError={() => setFailedLogo(name)}
        />
      ) : (
        <Plug size={18} />
      )}
    </span>
  );
}
