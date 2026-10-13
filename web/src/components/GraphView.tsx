import {
  Background,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo } from 'react'
import type { GraphInfo, Run } from '../types'

const NODE_W = 150
const SERVER_W = 230
const RANK_GAP = 90

type FlowNodeData = { label: string; state: 'idle' | 'visited' | 'active'; visits: number; terminal: boolean }
type ServerNodeData = {
  name: string
  url: string
  tools: { name: string; calls: number; active: boolean }[]
  active: boolean
}

function FlowNode({ data }: NodeProps<Node<FlowNodeData>>) {
  return (
    <div className={`gnode ${data.state} ${data.terminal ? 'terminal' : ''}`}>
      <Handle type="target" position={Position.Top} />
      <Handle type="target" id="left-in" position={Position.Left} />
      <span>{data.label}</span>
      {data.visits > 1 && <span className="visits">×{data.visits}</span>}
      <Handle type="source" position={Position.Bottom} />
      <Handle type="source" id="left-out" position={Position.Left} />
    </div>
  )
}

function ServerNode({ data }: NodeProps<Node<ServerNodeData>>) {
  return (
    <div className={`snode ${data.active ? 'active' : ''}`}>
      <Handle type="target" position={Position.Top} />
      <div className="snode-head">
        <span className="badge">MCP</span> {data.name}
      </div>
      <div className="snode-url">{data.url}</div>
      <ul>
        {data.tools.map((t) => (
          <li key={t.name} className={t.active ? 'active' : t.calls ? 'visited' : ''}>
            <code>{t.name}</code>
            {t.calls > 0 && <span className="visits">×{t.calls}</span>}
          </li>
        ))}
      </ul>
    </div>
  )
}

const nodeTypes = { flow: FlowNode, server: ServerNode }

const LABELS: Record<string, string> = { __start__: 'START', __end__: 'END' }

export function GraphView({ info, run }: { info: GraphInfo; run: Run | null }) {
  // 레이아웃: __start__ 기준 BFS 랭크로 세로 배치, 같은 랭크는 가로로 나열 (END는 오른쪽)
  const layout = useMemo(() => {
    const rank: Record<string, number> = { __start__: 0 }
    const queue = ['__start__']
    while (queue.length) {
      const n = queue.shift()!
      for (const e of info.edges)
        if (e.source === n && rank[e.target] === undefined) {
          rank[e.target] = rank[n] + 1
          queue.push(e.target)
        }
    }
    const byRank: Record<number, string[]> = {}
    for (const n of info.nodes) (byRank[rank[n.id] ?? 0] ??= []).push(n.id)
    const pos: Record<string, { x: number; y: number }> = {}
    for (const [r, ids] of Object.entries(byRank)) {
      ids.sort((a, b) => Number(a === '__end__') - Number(b === '__end__'))
      ids.forEach((id, i) => {
        pos[id] = { x: (i - (ids.length - 1) / 2) * 240, y: Number(r) * RANK_GAP }
      })
    }
    return { rank, pos, maxRank: Math.max(...Object.values(rank)) }
  }, [info])

  const { nodes: computed, edges } = useMemo(() => {
    const traversed = new Set(run?.traversed ?? [])
    const lastEdge = run && !run.endTs ? run.traversed.at(-1) : undefined
    const activeServer = run?.activeTool ? info.tools.find((t) => t.name === run.activeTool)?.server : undefined

    const nodes: Node[] = info.nodes.map((n) => ({
      id: n.id,
      type: 'flow',
      position: { x: layout.pos[n.id].x - NODE_W / 2, y: layout.pos[n.id].y },
      data: {
        label: LABELS[n.id] ?? n.id,
        terminal: n.id in LABELS,
        visits: run?.nodeVisits[n.id] ?? 0,
        state:
          run?.activeNode === n.id
            ? 'active'
            : run?.nodeVisits[n.id] || [...traversed].some((t) => t.endsWith(`->${n.id}`) || t.startsWith(`${n.id}->`))
              ? 'visited'
              : 'idle',
      } satisfies FlowNodeData,
    }))

    const edges: Edge[] = info.edges.map((e) => {
      const key = `${e.source}->${e.target}`
      const back = layout.rank[e.target] < layout.rank[e.source]
      const hit = traversed.has(key)
      return {
        id: key,
        source: e.source,
        target: e.target,
        sourceHandle: back ? 'left-out' : undefined,
        targetHandle: back ? 'left-in' : undefined,
        type: back ? 'smoothstep' : 'default',
        animated: key === lastEdge,
        label: e.conditional ? (e.target === '__end__' ? '답변 완료' : 'tool_calls') : undefined,
        className: `${hit ? 'hit' : ''} ${e.conditional ? 'conditional' : ''}`,
        markerEnd: { type: MarkerType.ArrowClosed },
      }
    })

    // MCP 서버 노드: tools 노드 아래에 배치
    const toolsPos = layout.pos['tools'] ?? { x: 0, y: layout.maxRank * RANK_GAP }
    info.servers.forEach((s, i) => {
      const id = `mcp:${s.name}`
      nodes.push({
        id,
        type: 'server',
        position: {
          x: toolsPos.x + (i - (info.servers.length - 1) / 2) * (SERVER_W + 30) - SERVER_W / 2,
          y: toolsPos.y + 115,
        },
        data: {
          name: s.name,
          url: s.url,
          active: activeServer === s.name,
          tools: info.tools
            .filter((t) => t.server === s.name)
            .map((t) => ({ name: t.name, calls: run?.toolCalls[t.name] ?? 0, active: run?.activeTool === t.name })),
        } satisfies ServerNodeData,
      })
      const used = info.tools.some((t) => t.server === s.name && run?.toolCalls[t.name])
      edges.push({
        id: `tools->${id}`,
        source: 'tools',
        target: id,
        animated: activeServer === s.name,
        className: `mcp ${used ? 'hit' : ''}`,
        label: 'streamable HTTP',
      })
    })
    return { nodes, edges }
  }, [info, run, layout])

  // React Flow는 측정(measured)된 노드만 표시하므로, 매 이벤트마다 새 노드 객체를 넘기지 않고
  // 기존 노드의 측정값을 유지한 채 data/position만 갱신한다.
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  useEffect(() => {
    setNodes((prev) => {
      const byId = new Map(prev.map((n) => [n.id, n]))
      return computed.map((n) => {
        const old = byId.get(n.id)
        return old ? { ...old, data: n.data, position: n.position } : n
      })
    })
  }, [computed, setNodes])

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.06, maxZoom: 1.1 }}
      nodesDraggable={false}
      nodesConnectable={false}
      proOptions={{ hideAttribution: true }}
      colorMode="system"
    >
      <Background gap={20} size={1} />
    </ReactFlow>
  )
}
