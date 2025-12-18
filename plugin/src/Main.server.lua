local DeltaManager = require(script.Parent.DeltaManager)

task.wait(5) -- Wait for Studio to finish loading content and rendering
DeltaManager.init()
