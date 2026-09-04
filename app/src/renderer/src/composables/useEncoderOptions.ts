/** 编码/解码/音轨/字幕选项与提示（新建任务页用）。
 *  按设备能力（store.hardware）与单文件实测（probe.decoder）出禁用态。 */
import { computed, watch } from 'vue'
import type { Ref } from 'vue'
import type { ProbeInfo } from '../api'
import { store } from '../store'

export function useEncoderOptions(
  probeInfo: Ref<ProbeInfo | null>,
  container: Ref<'mp4' | 'mkv' | 'mov'>,
  audioMode: Ref<string>,
  outKind: Ref<'video' | 'png' | 'jpg'>,
) {
  const nvencOk = computed(() => store.hardware?.nvenc ?? false)
  const av1NvencOk = computed(() => store.hardware?.av1_nvenc ?? false)
  const amfOk = computed(() => store.hardware?.amf ?? false)
  const svtOk = computed(() => store.hardware?.svt_av1 ?? false)
  const codecOptions = computed(() => [
    { label: 'H.264 · 软件编码（兼容性好）', value: 'h264' },
    { label: nvencOk.value ? 'H.264 · 硬件编码 NVENC（速度优先）' : 'H.264 · 硬件编码（当前设备不支持）', value: 'h264_nvenc', disabled: !nvencOk.value },
    { label: 'H.265 · 软件编码（体积较小）', value: 'h265' },
    { label: nvencOk.value ? 'H.265 · 硬件编码 NVENC（速度与体积均衡）' : 'H.265 · 硬件编码（当前设备不支持）', value: 'hevc_nvenc', disabled: !nvencOk.value },
    { label: av1NvencOk.value ? 'AV1 · 硬件编码 NVENC（RTX 40 系及以上，体积最小）' : 'AV1 · 硬件编码（当前设备不支持）', value: 'av1_nvenc', disabled: !av1NvencOk.value },
    { label: amfOk.value ? 'H.264 · 硬件编码 AMF（AMD 显卡）' : 'H.264 · 硬件编码 AMF（需 AMD 显卡）', value: 'h264_amf', disabled: !amfOk.value },
    { label: amfOk.value ? 'H.265 · 硬件编码 AMF（AMD 显卡）' : 'H.265 · 硬件编码 AMF（需 AMD 显卡）', value: 'hevc_amf', disabled: !amfOk.value },
    { label: svtOk.value ? 'AV1 · 软件编码 SVT（体积小，速度中等）' : 'AV1 · 软件编码 SVT（当前环境不支持）', value: 'av1_svt', disabled: !svtOk.value },
  ])
  const containerOptions = [
    { label: 'MP4（兼容性最好）', value: 'mp4' },
    { label: 'MKV（字幕/音频全兼容）', value: 'mkv' },
    { label: 'MOV（QuickTime）', value: 'mov' },
  ]
  // 解码器：单文件按本文件实测（probe 返回 decoder map，含源编码是否支持）；
  // 批量无逐文件探测，按设备能力兜底——个别文件不支持时 worker 端回退软解并记日志
  const probedDecoder = computed(() => probeInfo.value?.decoder ?? null)
  const nvdecOk = computed(() =>
    probedDecoder.value ? probedDecoder.value.nvdec : (store.hardware?.nvdec ?? false))
  const d3d11vaOk = computed(() =>
    probedDecoder.value ? probedDecoder.value.d3d11va : (store.hardware?.d3d11va ?? false))
  const decoderOptions = computed(() => [
    { label: '软件解码（默认，兼容性最好）', value: 'sw' },
    { label: nvdecOk.value ? '硬件解码 NVDEC（NVIDIA 显卡）' : '硬件解码 NVDEC（当前设备/视频不支持）', value: 'nvdec', disabled: !nvdecOk.value },
    { label: d3d11vaOk.value ? '硬件解码 D3D11VA（AMD / Intel 显卡）' : '硬件解码 D3D11VA（当前设备/视频不支持）', value: 'd3d11va', disabled: !d3d11vaOk.value },
  ])
  const audioOptions = computed(() => [
    { label: '自动（兼容则原样保留）', value: 'auto' },
    { label: '原样保留（copy，需容器兼容）', value: 'copy' },
    { label: '转 AAC 192k（最兼容）', value: 'aac' },
    { label: container.value === 'mkv' ? 'FLAC 无损（仅 MKV）' : 'FLAC 无损（切到 MKV 可用）', value: 'flac', disabled: container.value !== 'mkv' },
    { label: '不保留音轨', value: 'none' },
  ])
  watch(container, (c) => {
    if (c !== 'mkv' && audioMode.value === 'flac') audioMode.value = 'auto'
  })
  const srcSubs = computed(() => probeInfo.value?.subtitles ?? [])
  const subHint = computed(() => {
    if (!srcSubs.value.length) return ''
    if (container.value === 'mkv') return `MKV 将原样保留全部 ${srcSubs.value.length} 条字幕轨与内嵌字体`
    return 'MP4/MOV 仅支持文本字幕（转 mov_text），图形字幕（PGS 等）会被丢弃'
  })
  const audioHint = computed(() => {
    const n = probeInfo.value?.audio_tracks?.length ?? 0
    if (n < 2 || outKind.value !== 'video') return ''
    if (audioMode.value === 'none') return '不保留音轨'
    const conv = container.value === 'mkv' ? '' : '，不兼容 MP4/MOV 的轨自动转 AAC'
    return `将保留全部 ${n} 条音轨${conv}`
  })

  /** 媒体属性提示（信息卡下方标签行）：10bit/VFR/隔行——后两者后端会自动处理，
   * 展示让用户知道有这一步；隔行另给反交错建议（与智能推荐同依据，未开推荐也可见） */
  const mediaFlags = computed(() => {
    const p = probeInfo.value
    if (!p?.ok) return [] as string[]
    const flags: string[] = []
    if (p.field_order && p.field_order !== 'progressive') {
      flags.push('隔行扫描源 — 建议开启反交错')
    }
    if (p.vfr) flags.push('可变帧率 — 将自动转为恒定帧率')
    if ((p.bit_depth ?? 8) > 8) flags.push(`${p.bit_depth}bit 位深 — 处理时转为 8bit`)
    return flags
  })

  return {
    nvencOk, av1NvencOk, amfOk, svtOk, codecOptions, containerOptions,
    probedDecoder, nvdecOk, d3d11vaOk, decoderOptions, audioOptions,
    srcSubs, subHint, audioHint, mediaFlags,
  }
}
