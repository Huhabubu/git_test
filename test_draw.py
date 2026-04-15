import turtle

# 创建海龟对象
t = turtle.Turtle()
t.speed(0)          # 最快绘制速度
t.pensize(2)        # 画笔粗细
t.hideturtle()      # 隐藏海龟箭头，让图案更干净

# 设置屏幕
screen = turtle.Screen()
screen.bgcolor("black")   # 黑色背景，更显对称图案的美感
screen.title("对称几何图案 - 旋转正方形组合")

# 颜色列表，营造彩虹对称效果
colors = ["#FF0000", "#FF7F00", "#FFFF00", "#00FF00", "#0000FF", "#8B00FF"]

# 绘制对称几何图案（36次旋转，每次10度，形成360°完美旋转对称）
for i in range(36):
    # 选择颜色，实现彩色对称
    t.color(colors[i % len(colors)])
    
    # 绘制一个正方形（4条边）
    for _ in range(4):
        t.forward(120)      # 正方形边长
        t.right(90)
    
    # 每次绘制完一个正方形后旋转10度，形成高阶旋转对称
    t.right(10)

# 结束绘制
turtle.done()
