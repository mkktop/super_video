/** 处理时机设置（定时/闲时闸门：只拦“开始下一个任务”，不打断进行中）。 */
import { ref } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../api'

export const scheduleOptions = [
  { label: '立即处理', value: 'always' },
  { label: '指定时段', value: 'window' },
  { label: '电脑空闲时', value: 'idle' },
]

const isValidHHMM = (s: string) => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(s.trim())
  if (!m) return false
  const h = Number(m[1]), mi = Number(m[2])
  return h <= 23 && mi <= 59
}

export function useScheduleGate() {
  const message = useMessage()
  const queueSchedule = ref<'always' | 'window' | 'idle'>('always')
  const scheduleStart = ref('22:00')
  const scheduleEnd = ref('08:00')
  const idleMinutes = ref(15)
  const savingSchedule = ref(false)

  function apply(s: Record<string, unknown>) {
    queueSchedule.value = (s.queue_schedule as typeof queueSchedule.value) ?? 'always'
    scheduleStart.value = String(s.schedule_start ?? '22:00')
    scheduleEnd.value = String(s.schedule_end ?? '08:00')
    idleMinutes.value = Math.min(240, Math.max(1, Math.round(Number(s.idle_minutes) || 15)))
  }

  async function saveSchedule() {
    if (queueSchedule.value === 'window'
      && (!isValidHHMM(scheduleStart.value) || !isValidHHMM(scheduleEnd.value))) {
      message.error('时段需为 HH:MM 格式（如 22:00）')
      return
    }
    savingSchedule.value = true
    const r = await api.saveSettings({
      queue_schedule: queueSchedule.value,
      schedule_start: scheduleStart.value.trim(),
      schedule_end: scheduleEnd.value.trim(),
      idle_minutes: Math.round(idleMinutes.value) || 15,
    })
    savingSchedule.value = false
    if (r.ok) {
      message.success('已保存，下一个任务起按此规则领取（进行中的任务不受影响）')
    } else {
      message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    }
  }

  return { queueSchedule, scheduleStart, scheduleEnd, idleMinutes, savingSchedule, scheduleOptions, saveSchedule, apply }
}
