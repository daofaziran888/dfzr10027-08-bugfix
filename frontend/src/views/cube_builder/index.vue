<template>
  <section class="page" data-module="cube_builder">
    <header class="page-head">
      <div><h2>多维立方体</h2><p class="page-desc">维护多维数据立方体的建模定义、预计算粒度和存储策略，支撑OLAP多维交叉分析。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记数据立方体</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
        <button class="btn" type="button" :data-testid="`build-${selectedNode.id}`" @click="buildSnapshot(selectedNode)">构建快照</button>
      </div>
      <div v-if="selectedNode.snapshot" class="snapshot-panel" data-testid="snapshot-detail" style="grid-column:1/-1; margin-top:12px;">
        <h4>立方体快照 {{ selectedNode.snapshot.snapshot_version || '—' }}</h4>
        <p data-testid="snapshot-status">
          状态：{{ selectedNode.snapshot.status || '—' }}
          · 实际维度：{{ (selectedNode.snapshot.dimensions || []).join('、') || '无' }}
          · 分组数：{{ selectedNode.snapshot.cell_count ?? 0 }}
        </p>
        <p v-if="selectedNode.snapshot.missing_dimensions?.length" class="error-text" data-testid="snapshot-missing">
          缺失维度：{{ selectedNode.snapshot.missing_dimensions.join('、') }}（这些维度未参与聚合，不能当作完整多维结果）
        </p>
        <p v-if="selectedNode.snapshot.coverage" data-testid="snapshot-coverage">
          请求维度：{{ (selectedNode.snapshot.requested_dimensions || []).join('、') || '—' }}
          · 实际分组：{{ (selectedNode.snapshot.dimensions || []).join('、') || '无' }}
          · 覆盖完整：{{ selectedNode.snapshot.coverage.complete ? '是' : '否' }}
        </p>
        <table v-if="selectedNode.snapshot.cells?.length" class="data-table" data-testid="snapshot-cells">
          <thead>
            <tr><th v-for="field in selectedNode.snapshot.dim_fields" :key="field">{{ field }}</th><th>订单数</th><th>gmv</th><th>uv</th></tr>
          </thead>
          <tbody>
            <tr v-for="(cell, index) in selectedNode.snapshot.cells" :key="index">
              <td v-for="field in selectedNode.snapshot.dim_fields" :key="field">{{ cell[field] ?? '—' }}</td>
              <td>{{ cell['订单数'] }}</td>
              <td>{{ cell['gmv'] }}</td>
              <td>{{ cell['uv'] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记数据立方体</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot"><span>{{ graph.nodes.length }} 个节点</span><span v-if="errorMessage" class="error-text">{{ errorMessage }}</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/cube_builder'
const columns = ["立方体编号", "事实来源", "维度列表", "度量列表", "预计算粒度", "存储策略", "数据量级", "最后刷新"]
const actions = ["启动构建", "标记可用", "标记过期"]
const statuses = ["草稿", "构建中", "可用", "已过期"]
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const creating = ref(false); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) {
  if (selectedNode.value?.id === node.id) { selectedNode.value = null; return }
  selectedNode.value = node
  void selectBuiltNode(node.id)
}
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) { errorMessage.value = '加载失败' }
}
async function runAction(action: string, node: any) {
  try {
    const res = await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.message || '操作失败')
    errorMessage.value = action === '启动构建' ? (payload.message || '') : ''
    await reload()
    if (action === '启动构建') await selectBuiltNode(node.id)
  } catch (e: any) { errorMessage.value = e.message }
}
async function selectBuiltNode(id: number) {
  try {
    const res = await request(`${ENDPOINT}/${id}`)
    if (!res.ok) return
    selectedNode.value = await res.json()
  } catch (e) {
    errorMessage.value = '快照详情读取失败'
  }
}
async function buildSnapshot(node: any) {
  try {
    const res = await request(`${ENDPOINT}/${node.id}/build`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.message || '构建失败')
    errorMessage.value = payload.entry?.构建提示 || payload.message || ''
    await reload()
    await selectBuiltNode(node.id)
  } catch (e: any) { errorMessage.value = e.message }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
