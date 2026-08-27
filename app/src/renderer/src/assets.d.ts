// 资产模块声明（png 等）——独立文件确保进入编译图
declare module '*.png' {
  const src: string
  export default src
}
