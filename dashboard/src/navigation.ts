import {
  Activity, AudioLines, Bot, BrainCircuit, Cpu, Gauge, MessageSquareText, Mic, Network, Sparkles, Wrench,
} from 'lucide-react'

export const navGroups = [
  {
    label: 'Start here',
    items: [
      ['setup', Wrench, 'Getting started'],
      ['overview', Activity, 'System overview'],
    ],
  },
  {
    label: 'Build',
    items: [
      ['chat', MessageSquareText, 'Chat'],
      ['speech', AudioLines, 'Speech'],
      ['voice', Mic, 'Realtime voice'],
      ['vision', Bot, 'Robot vision'],
      ['studio', Sparkles, 'Studio'],
      ['agentlab', Bot, 'Agent Lab'],
    ],
  },
  {
    label: 'Operate',
    items: [
      ['models', BrainCircuit, 'Models'],
      ['jobs', Gauge, 'Jobs'],
      ['health', Cpu, 'Health & diagnostics'],
      ['logs', Activity, 'Logs'],
      ['maintenance', Wrench, 'Maintenance'],
      ['network', Network, 'Network'],
      ['system', Wrench, 'API'],
    ],
  },
] as const
