import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const EnvsView = {
  setup() {
    const items = ref([]); const threshold = ref(3); const err = ref("");
    async function reload() {
      try {
        const res = await api("/api/envs");
        items.value = res.items || [];
        threshold.value = res.high_risk_threshold ?? 3;
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { items, threshold, err, reload };
  },
  template: `
  <div class="panel">
    <h2>环境</h2>
    <p class="hint">风险等级 ≥ {{ threshold }} 为高风险环境（当前即 prod）：正式发布（含影子升正）强制审批号同事务落库，缺号整笔拒写、正式零增。</p>
    <div class="err" v-if="err">{{ err }}</div>
    <table>
      <thead><tr><th>代码</th><th>名称</th><th>风险等级</th><th>发布口径</th></tr></thead>
      <tbody>
        <tr v-for="it in items" :key="it.id">
          <td><code>{{ it.code }}</code></td>
          <td>{{ it.title }}</td>
          <td>{{ it.risk_level }}</td>
          <td>
            <span class="pill danger" v-if="it.high_risk">高风险 · 须审批号</span>
            <span class="pill" v-else>常规发布</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>`
};
