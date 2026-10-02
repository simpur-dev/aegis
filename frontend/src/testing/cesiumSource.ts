/**
 * 读**已安装的** Cesium 源码做漂移门禁。
 *
 * 为什么读源码而不是把结论抄进测试：本项目的地图链路依赖 Cesium 的几条私有前提
 * （影像矩形必须被切片方案完全包含、quantized-mesh 的解析结果放在哪些字段上、
 * layer.json 的 `available` 怎么翻 y）。这些前提一旦变了，正确动作是改我们的代码，
 * 而不是把断言删掉。抄一份"我以为它长这样"的清单只能证明两份抄写一致，证明不了它没变。
 */

import { readRepoFile } from './repoSource'

/** 取 `node_modules/@cesium/engine/Source/**` 下的某个文件；路径按仓库根解析，缺文件就响亮报错。 */
export function cesiumEngineSource(...parts: string[]): string {
  return readRepoFile('frontend', 'node_modules', '@cesium', 'engine', 'Source', ...parts)
}

/**
 * 摊平成一行好做子串断言。行首的 `//` 必须一起去掉：注释里的句子会被斜杠切成几段，
 * 于是断言假红（这条我们踩过一次）。
 */
export function flattenSource(text: string): string {
  return text
    .split('\n')
    .map((line) => line.replace(/^\s*\/\/\s*/, ''))
    .join(' ')
    .replace(/\s+/g, ' ')
}

export function cesiumSourceFlat(...parts: string[]): string {
  return flattenSource(cesiumEngineSource(...parts))
}
