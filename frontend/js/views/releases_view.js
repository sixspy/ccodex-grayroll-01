import { api } from "../api.js";
const { ref, onMounted, computed } = Vue;
export const ReleasesView = {
  setup() {
    const official = ref([]); const shadows = ref([]); const envs = ref([]);
    const form = ref({
      flag_id: 3, env_id: 1, kind: "percent", percent: 30, cohort_id: null,
      summary: "开发环境三成灰度试跑", use_shadow: false, approval_no: "",
    });
    const err = ref(""); const msg = ref("");
    async function reload() {
      const res = await api("/api/releases");
      official.value = res.official || []; shadows.value = res.shadows || [];
      const envRes = await api("/api/envs");
      envs.value = envRes.items || [];
    }
    const selectedEnv = computed(() => envs.value.find(e => e.id === form.value.env_id));
    const highRisk = computed(() => !!selectedEnv.value?.high_risk);
    async function publish() {
      err.value = ""; msg.value = "";
      try {
        const body = {
          ...form.value,
          cohort_id: form.value.cohort_id || null,
          approval_no: highRisk.value ? (form.value.approval_no || "").trim() : null,
        };
        const res = await api("/api/releases/publish", { method: "POST", body: JSON.stringify(body) });
        msg.value = `已提交：${res.mode}${res.approval_no ? "（审批号 " + res.approval_no + " 同事务落库）" : ""}`;
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(id) {
      err.value = "";
      try {
        const payload = highRisk.value ? { approval_no: (form.value.approval_no || "").trim() } : {};
        await api(`/api/releases/shadow/${id}/promote`, { method: "POST", body: JSON.stringify(payload) });
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { official, shadows, envs, form, err, msg, highRisk, selectedEnv, publish, promote, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>发布生效</h2>
      <p class="hint">默认甲路径：同事务写正式配置并升版本。高风险环境（prod）正式发布强制审批号，缺号整笔拒写、正式零增；勾选影子则走乙路径草案。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>类型</label>
        <select v-model="form.kind"><option>percent</option><option>cohort</option><option>percent_and_cohort</option></select>
      </div>
      <div class="field"><label>比例</label><input type="number" v-model.number="form.percent" /></div>
      <div class="field"><label>说明</label><textarea v-model="form.summary" rows="2"></textarea></div>
      <div class="field" v-if="highRisk">
        <label>审批号（高风险必填，同事务双写）</label>
        <input v-model="form.approval_no" placeholder="如 APR-2026-0001" />
        <span class="pill danger" style="margin-top:0.3rem">高风险 · 缺审批号将整笔拒写</span>
      </div>
      <div class="field"><label><input type="checkbox" v-model="form.use_shadow" /> 使用影子草案</label></div>
      <button class="btn" @click="publish">提交发布</button>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>正式发布</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>版本</th><th>审批号</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="r in official" :key="r.id">
            <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>v{{ r.version }}</td>
            <td>
              <span class="pill danger" v-if="r.env_risk_level >= 3 && !r.ledger_approval_no">未审批</span>
              <code v-else-if="r.approval_no">{{ r.approval_no }}</code>
              <span class="muted" v-else>—</span>
            </td>
            <td>{{ r.summary }}</td>
          </tr>
        </tbody></table>
      <h2 style="margin-top:1rem">影子发布</h2>
      <table><thead><tr><th>ID</th><th>开关</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="s in shadows" :key="'s'+s.id">
            <td>{{ s.id }}</td><td>{{ s.flag_id }}</td><td>{{ s.status }}</td>
            <td><button class="btn secondary" v-if="s.status==='open'" @click="promote(s.id)">升正</button></td>
          </tr>
        </tbody></table>
    </div>
  </div>`
};
